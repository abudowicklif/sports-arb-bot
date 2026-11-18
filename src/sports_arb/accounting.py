"""Capital ledger: realized-profit recognition, reinvestment split, drawdown protection.

Rules enforced here:
* Profit is recognized once per ``ref`` (UNIQUE), so replays/restarts cannot double count.
* Only realized net PnL feeds the account; unrealized or locked capital never does.
* Losses reduce undistributed profit first, then impair operating capital.
* Distribution is blocked while suspended (drawdown) or while undistributed <= liabilities.
* Policy changes need the authorization phrase and are written to the audit trail.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from sports_arb.paths import DATA_DIR

ZERO = Decimal("0")
HUNDRED = Decimal("100")
AUTH_PHRASE = "AUTHORIZE POLICY CHANGE"
DEFAULT_POLICY = Path(__file__).resolve().parents[2] / "config" / "strategy.toml"


class CapitalError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CapitalPolicy:
    reinvest_pct: Decimal = Decimal("60")
    withdraw_pct: Decimal = Decimal("25")
    reserve_pct: Decimal = Decimal("15")
    max_drawdown_pct: Decimal = Decimal("10")
    max_trade_fraction: Decimal = Decimal("0.25")
    max_daily_loss: Decimal = Decimal("10")
    max_weekly_loss: Decimal = Decimal("20")

    def __post_init__(self) -> None:
        if self.reinvest_pct + self.withdraw_pct + self.reserve_pct != HUNDRED:
            raise CapitalError("reinvest + withdraw + reserve must equal 100")
        if min(self.reinvest_pct, self.withdraw_pct, self.reserve_pct) < ZERO:
            raise CapitalError("allocation percentages must be non-negative")
        if not ZERO < self.max_drawdown_pct <= HUNDRED:
            raise CapitalError("max_drawdown_pct must be in (0, 100]")
        if not ZERO < self.max_trade_fraction <= Decimal("1"):
            raise CapitalError("max_trade_fraction must be in (0, 1]")

    @classmethod
    def load(cls, path: Path = DEFAULT_POLICY) -> CapitalPolicy:
        if not path.exists():
            return cls()
        data = tomllib.loads(path.read_text()).get("capital", {})
        return cls(**{k: Decimal(str(v)) for k, v in data.items() if k in cls.__slots__})

    def digest(self) -> str:
        blob = json.dumps({k: str(getattr(self, k)) for k in self.__slots__}, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


SCHEMA = """
CREATE TABLE IF NOT EXISTS capital_events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  kind TEXT NOT NULL,         -- DEPOSIT, REALIZED_PNL, ALLOCATION, WITHDRAWAL, POLICY, ...+
  ref TEXT UNIQUE,            -- idempotency key (round id, allocation id, ...)
  amount TEXT NOT NULL DEFAULT '0',
  detail TEXT NOT NULL DEFAULT '{}'
);
"""


class CapitalAccount:
    def __init__(
        self,
        path: Path = DATA_DIR / "capital.db",
        policy: CapitalPolicy | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.clock = clock
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        self.policy = policy or CapitalPolicy.load()

    def _add(self, kind: str, ref: str | None, amount: Decimal, detail: dict | None = None) -> bool:
        try:
            self.db.execute(
                "INSERT INTO capital_events (ts, kind, ref, amount, detail) VALUES (?,?,?,?,?)",
                (self.clock(), kind, ref, str(amount), json.dumps(detail or {}, default=str)),
            )
            self.db.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def _sum(self, kind: str) -> Decimal:
        rows = self.db.execute("SELECT amount FROM capital_events WHERE kind=?", (kind,))
        return sum((Decimal(r[0]) for r in rows), ZERO)

    def _alloc(self, bucket: str) -> Decimal:
        total = ZERO
        for _amount, detail in self.db.execute(
            "SELECT amount, detail FROM capital_events WHERE kind='ALLOCATION'"
        ):
            total += Decimal(json.loads(detail).get(bucket, "0"))
        return total

    # ---- recording -------------------------------------------------------------------
    def deposit(self, amount: Decimal, ref: str) -> bool:
        if amount <= ZERO:
            raise CapitalError("deposit must be positive")
        return self._add("DEPOSIT", f"deposit:{ref}", amount)

    def recognize_pnl(self, ref: str, net_pnl: Decimal, detail: dict | None = None) -> bool:
        """Record a REALIZED net PnL (after fees and unwind costs). False if already recorded."""
        created = self._add("REALIZED_PNL", f"pnl:{ref}", net_pnl, detail)
        if created:
            self._check_drawdown()
        return created

    def record_withdrawal(self, amount: Decimal, ref: str) -> bool:
        if amount <= ZERO or amount > self.withdrawable():
            raise CapitalError("withdrawal exceeds withdrawable balance")
        return self._add("WITHDRAWAL", f"withdraw:{ref}", amount)

    # ---- balances --------------------------------------------------------------------
    def deposits(self) -> Decimal:
        return self._sum("DEPOSIT")

    def net_realized(self) -> Decimal:
        return self._sum("REALIZED_PNL")

    def total_equity(self) -> Decimal:
        return self.deposits() + self.net_realized() - self._sum("WITHDRAWAL")

    def reinvested(self) -> Decimal:
        return self._alloc("reinvest")

    def reserve(self) -> Decimal:
        return self._alloc("reserve")

    def withdrawable(self) -> Decimal:
        return self._alloc("withdraw") - self._sum("WITHDRAWAL")

    def undistributed(self) -> Decimal:
        allocated = self.reinvested() + self._alloc("withdraw") + self.reserve()
        return self.net_realized() - allocated

    def operating_capital(self) -> Decimal:
        """Trading base: deposits + reinvested profit, impaired by any undistributed deficit."""
        return self.deposits() + self.reinvested() + min(ZERO, self.undistributed())

    def max_trade_cost(self) -> Decimal:
        return (self.operating_capital() * self.policy.max_trade_fraction).quantize(
            Decimal("0.000001")
        )

    def peak_equity(self) -> Decimal:
        peak = running = ZERO
        for _kind, amount in self.db.execute(
            "SELECT kind, amount FROM capital_events "
            "WHERE kind IN ('DEPOSIT','REALIZED_PNL') ORDER BY seq"
        ):
            running += Decimal(amount)
            peak = max(peak, running)
        return peak

    def drawdown_pct(self) -> Decimal:
        peak = self.peak_equity()
        if peak <= ZERO:
            return ZERO
        current = self.deposits() + self.net_realized()
        return max(ZERO, (peak - current) / peak * HUNDRED)

    # ---- protection ------------------------------------------------------------------
    def suspended(self) -> bool:
        row = self.db.execute(
            "SELECT kind FROM capital_events "
            "WHERE kind IN ('SUSPEND','RESUME') ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return bool(row and row[0] == "SUSPEND")

    def realized_since(self, seconds: float) -> Decimal:
        cutoff = self.clock() - seconds
        rows = self.db.execute(
            "SELECT amount FROM capital_events WHERE kind='REALIZED_PNL' AND ts>=?", (cutoff,)
        )
        return sum((Decimal(r[0]) for r in rows), ZERO)

    def loss_limit_hit(self) -> str | None:
        if self.realized_since(86400) <= -self.policy.max_daily_loss:
            return "daily_loss_limit"
        if self.realized_since(7 * 86400) <= -self.policy.max_weekly_loss:
            return "weekly_loss_limit"
        return None

    def trading_allowed(self) -> bool:
        return (
            not self.suspended()
            and self.operating_capital() > ZERO
            and self.loss_limit_hit() is None
        )

    def _check_drawdown(self) -> None:
        if not self.suspended() and self.drawdown_pct() >= self.policy.max_drawdown_pct:
            self._add(
                "SUSPEND",
                f"suspend:{time.time_ns()}",
                ZERO,
                {"drawdown_pct": str(self.drawdown_pct())},
            )

    def resume(self, phrase: str) -> None:
        if phrase != AUTH_PHRASE:
            raise CapitalError("resume requires the authorization phrase")
        self._add("RESUME", f"resume:{time.time_ns()}", ZERO)

    # ---- distribution ----------------------------------------------------------------
    def allocate(self, liabilities: Decimal = ZERO, ref: str | None = None) -> dict[str, Decimal]:
        """Split eligible profit. Liabilities (open unhedged exposure, locked capital at risk)
        are held back first. Idempotent per ``ref``."""
        if self.suspended():
            raise CapitalError("distribution blocked: account suspended after drawdown")
        eligible = self.undistributed() - liabilities
        if eligible <= ZERO:
            return {"reinvest": ZERO, "withdraw": ZERO, "reserve": ZERO}
        q = Decimal("0.000001")
        reinvest = (eligible * self.policy.reinvest_pct / HUNDRED).quantize(q)
        withdraw = (eligible * self.policy.withdraw_pct / HUNDRED).quantize(q)
        reserve = eligible - reinvest - withdraw  # remainder: no rounding leak
        split = {"reinvest": reinvest, "withdraw": withdraw, "reserve": reserve}
        key = ref or f"alloc:{time.time_ns()}"
        self._add(
            "ALLOCATION",
            key,
            eligible,
            {k: str(v) for k, v in split.items()} | {"policy": self.policy.digest()},
        )
        return split

    def set_policy(self, new: CapitalPolicy, phrase: str) -> None:
        if phrase != AUTH_PHRASE:
            raise CapitalError("policy change requires the authorization phrase")
        self._add(
            "POLICY",
            f"policy:{time.time_ns()}",
            ZERO,
            {"old": self.policy.digest(), "new": new.digest()},
        )
        self.policy = new

    def pnl_by_strategy(self) -> dict[str, list[Decimal]]:
        """Realized per-trade PnL grouped by the strategy tag recorded with each recognition."""
        out: dict[str, list[Decimal]] = {}
        for amount, detail in self.db.execute(
            "SELECT amount, detail FROM capital_events WHERE kind='REALIZED_PNL' ORDER BY seq"
        ):
            tag = json.loads(detail).get("strategy", "untagged")
            out.setdefault(tag, []).append(Decimal(amount))
        return out

    def snapshot(self) -> dict[str, str]:
        pnl_rows = [
            Decimal(r[0])
            for r in self.db.execute("SELECT amount FROM capital_events WHERE kind='REALIZED_PNL'")
        ]
        wins = [p for p in pnl_rows if p > ZERO]
        losses = [p for p in pnl_rows if p < ZERO]
        n = len(pnl_rows)
        return {
            "deposits": str(self.deposits()),
            "total_realized_profit": str(sum(wins, ZERO)),
            "total_realized_loss": str(sum(losses, ZERO)),
            "net_cumulative_profit": str(self.net_realized()),
            "operating_capital": str(self.operating_capital()),
            "reinvested_profit": str(self.reinvested()),
            "withdrawable_profit": str(self.withdrawable()),
            "reserve_balance": str(self.reserve()),
            "undistributed_profit": str(self.undistributed()),
            "trades_recognized": str(n),
            "win_rate": str((Decimal(len(wins)) / n).quantize(Decimal("0.0001")) if n else ZERO),
            "avg_profit_per_trade": str(
                (self.net_realized() / n).quantize(Decimal("0.000001")) if n else ZERO
            ),
            "drawdown_pct": str(self.drawdown_pct().quantize(Decimal("0.01"))),
            "suspended": str(self.suspended()),
            "policy": self.policy.digest(),
        }


def main() -> None:
    p = argparse.ArgumentParser(prog="sports-arb-capital")
    p.add_argument("--db", type=Path, default=DATA_DIR / "capital.db")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    d = sub.add_parser("deposit")
    d.add_argument("amount")
    d.add_argument("ref")
    pnl = sub.add_parser("recognize")
    pnl.add_argument("ref")
    pnl.add_argument("net_pnl")
    al = sub.add_parser("allocate")
    al.add_argument("--liabilities", default="0")
    sp = sub.add_parser("set-policy")
    for k in ("reinvest", "withdraw", "reserve"):
        sp.add_argument(f"--{k}", required=True)
    sp.add_argument("--max-drawdown", required=True)
    sp.add_argument("--max-trade-fraction", required=True)
    sp.add_argument("--authorize", required=True)
    r = sub.add_parser("resume")
    r.add_argument("--authorize", required=True)
    a = p.parse_args()
    acct = CapitalAccount(a.db)
    if a.cmd == "deposit":
        acct.deposit(Decimal(a.amount), a.ref)
    elif a.cmd == "recognize":
        acct.recognize_pnl(a.ref, Decimal(a.net_pnl))
    elif a.cmd == "allocate":
        print(json.dumps({k: str(v) for k, v in acct.allocate(Decimal(a.liabilities)).items()}))
    elif a.cmd == "set-policy":
        acct.set_policy(
            CapitalPolicy(
                Decimal(a.reinvest),
                Decimal(a.withdraw),
                Decimal(a.reserve),
                Decimal(a.max_drawdown),
                Decimal(a.max_trade_fraction),
            ),
            a.authorize,
        )
    elif a.cmd == "resume":
        acct.resume(a.authorize)
    print(json.dumps(acct.snapshot(), indent=2))


if __name__ == "__main__":
    main()
