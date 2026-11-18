"""Deterministic replay, fill simulation and the profitability gate.

Nothing here reads the wall clock. Decisions use the snapshot at time t and fills are
tested against the first snapshot at or after ``t + latency + sports_delay``, using the
same detector, protected limits and capped-edge test as live execution.

Simulated results are NOT realized trading results.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sports_arb.book import OrderBook, OrderBookCache
from sports_arb.detector import SameMarketDetector
from sports_arb.errors import MarketDataError
from sports_arb.live_config import LiveRiskConfig
from sports_arb.models import ONE, ZERO, FeeSchedule, SportsMarket
from sports_arb.strategy import StrategyConfig, cap_plan, detector_config

MIN_TRADES_FOR_VERDICT = 30


def market_from_record(d: dict[str, Any]) -> SportsMarket:
    f = d["fee_schedule"]
    return SportsMarket(
        market_id=d["market_id"],
        condition_id=d["condition_id"],
        slug=d["slug"],
        question=d["question"],
        version=d["version"],
        outcome_labels=tuple(d["outcome_labels"]),
        asset_ids=tuple(d["asset_ids"]),
        sports_market_type=d["sports_market_type"],
        fee_schedule=FeeSchedule(
            rate=Decimal(f["rate"]),
            exponent=int(f["exponent"]),
            ceil_quantum=Decimal(f["ceil_quantum"]),
        ),
        order_min_size=Decimal(d["order_min_size"]),
        liquidity=Decimal(d["liquidity"]),
        volume_24h=Decimal(d["volume_24h"]),
        game_start_time=d["game_start_time"],
        seconds_delay=int(d["seconds_delay"]),
    )


def kalshi_books_from_raw(ticker: str, raw: dict[str, Any], recv: float, tick: Decimal):
    from sports_arb.kalshi_gateway import normalize_kalshi_books

    resp = SimpleNamespace(
        orderbook_fp=SimpleNamespace(
            yes_dollars=raw.get("yes_dollars", []), no_dollars=raw.get("no_dollars", [])
        )
    )
    return normalize_kalshi_books(ticker, resp, received_at=recv, tick_size=tick)


def read_records(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def phase(market: SportsMarket, ts: float) -> str:
    """pre | live | unknown. Unknown when the venue gives no real start time."""
    if not market.game_start_time:
        return "unknown"
    try:
        start = datetime.fromisoformat(market.game_start_time.replace("Z", "+00:00"))
    except ValueError:
        return "unknown"
    return "pre" if ts < start.timestamp() else "live"


@dataclass(slots=True)
class Snapshot:
    ts: float
    books: dict[str, OrderBook]


@dataclass(slots=True)
class Trade:
    ts: float
    market_id: str
    venue: str
    market_type: str
    phase: str
    status: str
    pnl: Decimal
    cap_cost: Decimal
    shares: Decimal
    latency_s: float


@dataclass(slots=True)
class DataQuality:
    snapshots: int = 0
    crossed_pairs: int = 0  # bid0 + bid1 > 1 : the only source of positive edge
    non_monotone: int = 0
    invalid_books: int = 0


@dataclass(slots=True)
class Result:
    latency_s: float
    max_pair_skew_ms: float
    candidates: int = 0
    candidate_status: Counter = field(default_factory=Counter)
    trades: list[Trade] = field(default_factory=list)
    rejected_cap: int = 0

    @property
    def net_pnl(self) -> Decimal:
        return sum((t.pnl for t in self.trades), ZERO)


def buy_fok(book: OrderBook, shares: Decimal, limit: Decimal, fees: FeeSchedule):
    left, cost, fee = shares, ZERO, ZERO
    for lvl in book.asks:
        if lvl.price > limit:
            break
        take = min(left, lvl.size)
        cost += take * lvl.price
        fee += fees.fee(take, lvl.price)
        left -= take
        if left == ZERO:
            return cost, fee
    return None


def sell_fak(book: OrderBook, shares: Decimal, fees: FeeSchedule):
    left, proceeds, fee = shares, ZERO, ZERO
    for lvl in book.bids:
        if left == ZERO:
            break
        take = min(left, lvl.size)
        proceeds += take * lvl.price
        fee += fees.fee(take, lvl.price)
        left -= take
    return proceeds, fee, left


def simulate_fill(market, shares, limits, later: Snapshot | None) -> tuple[str, Decimal]:
    """Independent legs against the later book. Returns (status, pnl_at_resolution)."""
    if later is None:
        return "NO_LATER_SNAPSHOT", ZERO
    b0, b1 = (later.books.get(a) for a in market.asset_ids)
    if b0 is None or b1 is None:
        return "BOOK_MISSING", ZERO
    f0 = buy_fok(b0, shares, limits[0], market.fee_schedule)
    f1 = buy_fok(b1, shares, limits[1], market.fee_schedule)
    if f0 and f1:
        # A complementary pair pays exactly $1 at resolution (void/split resolutions also
        # pay $1 combined, but may lock capital longer).
        return "HEDGED", shares - (f0[0] + f1[0] + f0[1] + f1[1])
    if not f0 and not f1:
        return "NO_FILL", ZERO
    got, book = (f0, b0) if f0 else (f1, b1)
    proceeds, sfee, left = sell_fak(book, shares, market.fee_schedule)
    pnl = proceeds - sfee - got[0] - got[1]
    if left > ZERO:
        # Residual one-sided exposure: mark at zero (worst case) rather than flatter it.
        return "EXPOSED_MARKED_ZERO", pnl
    return "LEG_FAILED_UNWOUND", pnl


class SimRiskGate:
    """In-memory mirror of the ledger's daily notional and loss limits (explicit clock)."""

    def __init__(self, risk: LiveRiskConfig) -> None:
        self.risk = risk
        self.committed: dict[str, Decimal] = defaultdict(Decimal)
        self.pnl: dict[str, Decimal] = defaultdict(Decimal)

    @staticmethod
    def day(ts: float) -> str:
        return datetime.fromtimestamp(ts, UTC).date().isoformat()

    def allows(self, ts: float, cap_cost: Decimal) -> bool:
        d = self.day(ts)
        if self.committed[d] + cap_cost > self.risk.max_daily_notional:
            return False
        return not self.pnl[d] <= -self.risk.max_daily_realized_loss

    def reserve(self, ts: float, cap_cost: Decimal) -> None:
        self.committed[self.day(ts)] += cap_cost

    def release(self, ts: float, cap_cost: Decimal) -> None:
        self.committed[self.day(ts)] -= cap_cost

    def record_loss_if_flat(self, ts: float, status: str, pnl: Decimal) -> None:
        # Mirrors live: only flat/unwound outcomes feed the realized-loss halt.
        if status in {"LEG_FAILED_UNWOUND", "EXPOSED_MARKED_ZERO"}:
            self.pnl[self.day(ts)] += pnl


def load_snapshots(path: Path, *, kalshi_tick: Decimal = Decimal("0.01")):
    """Parse the recording once. Returns (universes, snapshots, resolutions, quality)."""
    universes: list[tuple[float, dict[str, SportsMarket]]] = []
    snaps: list[Snapshot] = []
    resolutions: dict[str, float] = {}
    q = DataQuality()
    last_ts = None
    for rec in read_records(path):
        kind = rec.get("k")
        if kind == "universe":
            universes.append(
                (rec["ts"], {d["market_id"]: market_from_record(d) for d in rec["markets"]})
            )
        elif kind == "resolution":
            resolutions[rec["market_id"]] = rec["ts"]
        elif kind in {"books", "kalshi_books"}:
            ts = rec["recv"]
            if last_ts is not None and ts < last_ts:
                q.non_monotone += 1
                continue
            last_ts = ts
            books: dict[str, OrderBook] = {}
            if kind == "books":
                for raw in rec["raw"]:
                    try:
                        b = OrderBook.from_payload(raw, received_at=ts)
                    except MarketDataError:
                        q.invalid_books += 1
                        continue
                    books[b.asset_id] = b
            else:
                for ticker, raw in rec["raw"].items():
                    try:
                        books.update(kalshi_books_from_raw(ticker, raw, ts, kalshi_tick))
                    except MarketDataError:
                        q.invalid_books += 1
            snaps.append(Snapshot(ts, books))
            q.snapshots += 1
    return universes, snaps, resolutions, q


def run_replay(
    universes,
    snaps: list[Snapshot],
    risk: LiveRiskConfig,
    strategy: StrategyConfig,
    *,
    latency_s: float,
    max_pair_skew_ms: float | None = None,
    quality: DataQuality | None = None,
) -> Result:
    skew = risk.max_pair_skew_ms if max_pair_skew_ms is None else max_pair_skew_ms
    result = Result(latency_s=latency_s, max_pair_skew_ms=skew)
    gate = SimRiskGate(risk)
    detectors: dict[str, SameMarketDetector] = {}
    held: set[str] = set()  # one open pair per market (no depth reuse)
    uni_i, markets = -1, {}
    for i, snap in enumerate(snaps):
        while uni_i + 1 < len(universes) and universes[uni_i + 1][0] <= snap.ts:
            uni_i += 1
            markets = universes[uni_i][1]
        cache = OrderBookCache()
        for b in snap.books.values():
            cache.put(b)
        for m in markets.values():
            venue = "kalshi" if m.version.startswith("kalshi") else "polymarket"
            if m.market_id in held:
                continue
            b0, b1 = (snap.books.get(a) for a in m.asset_ids)
            if b0 is None or b1 is None:
                continue
            if quality and b0.best_bid and b1.best_bid:
                if b0.best_bid.price + b1.best_bid.price > ONE:
                    quality.crossed_pairs += 1
            det = detectors.get(venue)
            if det is None:
                cfg = replace(detector_config(risk, strategy, venue=venue), max_pair_skew_ms=skew)
                det = detectors[venue] = SameMarketDetector(cfg)
            ev = det.evaluate(m, cache, now=snap.ts)
            if ev.opportunity is None:
                continue
            result.candidates += 1
            result.candidate_status[ev.status] += 1
            if ev.status != "qualifying":
                continue
            opp = ev.opportunity
            try:
                l0, l1, cap, cap_edge = cap_plan(
                    m,
                    opp.shares,
                    b0,
                    b1,
                    extra_cost=strategy.kalshi_cap_extra if venue == "kalshi" else ZERO,
                )
            except Exception:
                result.rejected_cap += 1
                continue
            if cap > risk.max_pair_cost or cap_edge < risk.min_net_edge_bps:
                result.rejected_cap += 1
                continue
            if not gate.allows(snap.ts, cap):
                continue
            gate.reserve(snap.ts, cap)
            fill_ts = snap.ts + latency_s + (m.seconds_delay if venue == "polymarket" else 0)
            later = next((s for s in snaps[i + 1 :] if s.ts >= fill_ts), None)
            status, pnl = simulate_fill(m, opp.shares, (l0, l1), later)
            if status in {"NO_FILL", "NO_LATER_SNAPSHOT", "BOOK_MISSING"}:
                gate.release(snap.ts, cap)
            else:
                held.add(m.market_id)
                gate.record_loss_if_flat(snap.ts, status, pnl)
            result.trades.append(
                Trade(
                    snap.ts,
                    m.market_id,
                    venue,
                    m.sports_market_type,
                    phase(m, snap.ts),
                    status,
                    pnl,
                    cap,
                    opp.shares,
                    latency_s,
                )
            )
    return result


def verdict(results_by_latency: dict[float, Result], target_latency: float = 2.0) -> str:
    """Never PASS on thin or non-positive evidence; never says 'proven'."""
    r = results_by_latency.get(target_latency)
    if r is None:
        return "INSUFFICIENT_DATA: target latency not run"
    filled = [
        t for t in r.trades if t.status in {"HEDGED", "LEG_FAILED_UNWOUND", "EXPOSED_MARKED_ZERO"}
    ]
    if len(filled) < MIN_TRADES_FOR_VERDICT:
        return f"INSUFFICIENT_DATA: {len(filled)} simulated entries < {MIN_TRADES_FOR_VERDICT}"
    if r.net_pnl <= ZERO:
        return f"FAIL: net simulated PnL {r.net_pnl} at {target_latency}s latency"
    ordered = sorted((t.pnl for t in filled), reverse=True)
    trimmed = sum(ordered[max(1, len(ordered) // 10) :], ZERO)
    if trimmed <= ZERO:
        return "FAIL: profit depends on the best 10% of trades"
    return (
        "PASS_PROVISIONAL: simulated only; confirm with forward paper recording "
        "and minimum-size live fills"
    )


def summarize(results: list[Result]) -> dict[str, Any]:
    out = []
    for r in results:
        by = defaultdict(lambda: [0, ZERO])
        for t in r.trades:
            for key in (f"phase={t.phase}", f"venue={t.venue}", f"type={t.market_type}"):
                by[key][0] += 1
                by[key][1] += t.pnl
        statuses = Counter(t.status for t in r.trades)
        out.append(
            {
                "latency_s": r.latency_s,
                "max_pair_skew_ms": r.max_pair_skew_ms,
                "candidates": r.candidates,
                "candidate_status": dict(r.candidate_status),
                "rejected_by_protected_limits": r.rejected_cap,
                "trade_status": dict(statuses),
                "trades": len(r.trades),
                "net_simulated_pnl": str(r.net_pnl),
                "breakdown": {k: {"n": v[0], "pnl": str(v[1])} for k, v in sorted(by.items())},
            }
        )
    return {"mode": "SIMULATION_NOT_REALIZED_RESULTS", "runs": out}
