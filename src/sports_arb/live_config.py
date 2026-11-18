"""Environment-only credentials and explicit live-trading arming."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from decimal import Decimal

from sports_arb.errors import SportsArbError
from sports_arb.paths import DATA_DIR

LIVE_ENV_FLAG = "POLYMARKET_LIVE_TRADING"
LIVE_CONFIRM_ENV = "POLYMARKET_LIVE_CONFIRM"
LIVE_PHRASE = "I UNDERSTAND REAL FUNDS ARE AT RISK"
KILL_FILE = DATA_DIR / "KILL_SWITCH"


class LiveConfigError(SportsArbError):
    pass


@dataclass(frozen=True, slots=True)
class Credentials:
    private_key: str
    api_key: str
    api_secret: str
    api_passphrase: str
    signature_type: int
    funder_address: str | None

    @classmethod
    def from_env(cls) -> Credentials:
        required = [
            "POLYMARKET_PRIVATE_KEY",
            "POLYMARKET_API_KEY",
            "POLYMARKET_API_SECRET",
            "POLYMARKET_API_PASSPHRASE",
        ]
        missing = [name for name in required if not os.environ.get(name)]
        if missing:
            raise LiveConfigError(f"missing environment variables: {', '.join(missing)}")
        private_key = os.environ["POLYMARKET_PRIVATE_KEY"]
        if not re.fullmatch(r"(?:0x)?[0-9a-fA-F]{64}", private_key):
            raise LiveConfigError("POLYMARKET_PRIVATE_KEY has an invalid format")
        try:
            signature_type = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", ""))
        except ValueError as exc:
            raise LiveConfigError("POLYMARKET_SIGNATURE_TYPE must be 0, 1, 2, or 3") from exc
        if signature_type not in (0, 1, 2, 3):
            raise LiveConfigError("POLYMARKET_SIGNATURE_TYPE must be 0, 1, 2, or 3")
        funder = os.environ.get("POLYMARKET_FUNDER_ADDRESS") or None
        if signature_type != 0:
            if not funder or not re.fullmatch(r"0x[0-9a-fA-F]{40}", funder):
                raise LiveConfigError(
                    "a valid POLYMARKET_FUNDER_ADDRESS is required for this signature type"
                )
        elif funder and not re.fullmatch(r"0x[0-9a-fA-F]{40}", funder):
            raise LiveConfigError("POLYMARKET_FUNDER_ADDRESS has an invalid format")
        return cls(
            private_key=private_key,
            api_key=os.environ["POLYMARKET_API_KEY"],
            api_secret=os.environ["POLYMARKET_API_SECRET"],
            api_passphrase=os.environ["POLYMARKET_API_PASSPHRASE"],
            signature_type=signature_type,
            funder_address=funder,
        )


@dataclass(frozen=True, slots=True)
class LiveRiskConfig:
    max_pair_cost: Decimal = Decimal("10")
    max_daily_notional: Decimal = Decimal("20")
    max_in_flight_cost: Decimal = Decimal("20")
    max_daily_realized_loss: Decimal = Decimal("1")
    max_unwind_loss: Decimal = Decimal("1")
    max_shares: Decimal = Decimal("5")
    min_net_edge_bps: Decimal = Decimal("50")
    min_net_profit: Decimal = Decimal("0.01")
    max_signal_age_ms: float = 1500.0
    max_pair_skew_ms: float = 500.0
    order_timeout_seconds: float = 12.0
    unwind_attempts: int = 3

    def validate(self) -> None:
        if not Decimal("0") < self.max_pair_cost <= Decimal("25"):
            raise LiveConfigError("max_pair_cost must be in (0, 25]")
        if not Decimal("0") < self.max_daily_notional <= Decimal("100"):
            raise LiveConfigError("max_daily_notional must be in (0, 100]")
        if not Decimal("0") < self.max_in_flight_cost <= Decimal("50"):
            raise LiveConfigError("max_in_flight_cost must be in (0, 50]")
        if self.max_in_flight_cost > self.max_daily_notional:
            raise LiveConfigError("max_in_flight_cost cannot exceed max_daily_notional")
        if not Decimal("0") < self.max_daily_realized_loss <= Decimal("10"):
            raise LiveConfigError("max_daily_realized_loss must be in (0, 10]")
        if not Decimal("0") < self.max_unwind_loss <= Decimal("10"):
            raise LiveConfigError("max_unwind_loss must be in (0, 10]")
        if not Decimal("0") < self.max_shares <= Decimal("25"):
            raise LiveConfigError("max_shares must be in (0, 25]")
        if self.unwind_attempts < 1 or self.unwind_attempts > 5:
            raise LiveConfigError("unwind_attempts must be between 1 and 5")


def assert_live_armed(confirmation: str) -> None:
    if os.environ.get(LIVE_ENV_FLAG) != "YES":
        raise LiveConfigError(f"set {LIVE_ENV_FLAG}=YES to arm real order submission")
    if os.environ.get(LIVE_CONFIRM_ENV) != LIVE_PHRASE:
        raise LiveConfigError(f"set {LIVE_CONFIRM_ENV} to the required risk phrase")
    if confirmation != "REAL FUNDS":
        raise LiveConfigError("live command requires --confirm-live 'REAL FUNDS'")
    if KILL_FILE.exists():
        raise LiveConfigError(
            f"kill switch is active at {KILL_FILE}; inspect/reconcile before removing it"
        )
