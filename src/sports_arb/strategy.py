"""Strategy numbers and entry gating shared by live execution and replay.

Live ``execute()`` and the backtest both call :func:`cap_plan`, so the protected
limits, capped cost and edge test cannot silently drift apart.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from sports_arb.book import OrderBook
from sports_arb.detector import DetectorConfig
from sports_arb.live_config import LiveRiskConfig
from sports_arb.models import BPS, ONE, SportsMarket

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "strategy.toml"


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    slippage_bps: Decimal = Decimal("25")
    kalshi_per_order_reserve: Decimal = Decimal("0.01")
    kalshi_cap_extra: Decimal = Decimal("0.02")

    @classmethod
    def load(cls, path: Path = DEFAULT_CONFIG) -> StrategyConfig:
        if not path.exists():
            return cls()
        section = tomllib.loads(path.read_text()).get("strategy", {})
        return cls(**{k: Decimal(str(v)) for k, v in section.items() if k in cls.__slots__})


def detector_config(
    risk: LiveRiskConfig, strategy: StrategyConfig, *, venue: str
) -> DetectorConfig:
    kalshi = venue == "kalshi"
    return DetectorConfig(
        min_shares=ONE,
        max_shares=risk.max_shares,
        max_capital=risk.max_pair_cost,
        min_net_edge_bps=risk.min_net_edge_bps,
        min_net_profit=risk.min_net_profit,
        slippage_bps=strategy.slippage_bps,
        max_book_age_ms=risk.max_signal_age_ms,
        max_pair_skew_ms=risk.max_pair_skew_ms,
        whole_contracts=kalshi,
        per_order_cost_reserve=strategy.kalshi_per_order_reserve if kalshi else Decimal("0"),
    )


def cap_plan(
    market: SportsMarket,
    shares: Decimal,
    book_0: OrderBook,
    book_1: OrderBook,
    *,
    extra_cost: Decimal = Decimal("0"),
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """Return (limit_0, limit_1, cap_cost, cap_edge_bps) exactly as live execution uses."""
    from sports_arb.live_execution import conservative_cost_at_limits, protected_buy_limit

    limit_0 = protected_buy_limit(book_0, shares)
    limit_1 = protected_buy_limit(book_1, shares)
    cap_cost = conservative_cost_at_limits(market, shares, limit_0, limit_1) + extra_cost
    return limit_0, limit_1, cap_cost, (shares - cap_cost) / shares * BPS
