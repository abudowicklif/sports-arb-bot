"""Fee- and depth-aware same-market complete-set arbitrage detector.

This module only produces theoretical indications. It does not place orders and
cannot prove that both legs will fill. A $1 payout assumes exactly one of the two
outcomes pays $1 under the market's final resolution rules; capital may remain
locked until merge/redeem/resolution.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from sports_arb.book import BookLevel, OrderBookCache
from sports_arb.models import BPS, ONE, ZERO, FeeSchedule, SportsMarket


@dataclass(frozen=True, slots=True)
class DetectorConfig:
    min_shares: Decimal = Decimal("5")
    max_shares: Decimal = Decimal("100")
    max_capital: Decimal = Decimal("100")
    min_net_edge_bps: Decimal = Decimal("20")
    min_net_profit: Decimal = Decimal("0.01")
    slippage_bps: Decimal = Decimal("10")
    settlement_cost_per_pair: Decimal = ZERO
    max_book_age_ms: float = 5000.0
    max_pair_skew_ms: float = 1000.0
    whole_contracts: bool = False
    per_order_cost_reserve: Decimal = ZERO


@dataclass(frozen=True, slots=True)
class Opportunity:
    market_id: str
    condition_id: str
    question: str
    sports_market_type: str
    outcome_labels: tuple[str, str]
    asset_ids: tuple[str, str]
    shares: Decimal
    best_ask_sum: Decimal
    gross_top_edge_bps: Decimal
    outcome_0_notional: Decimal
    outcome_1_notional: Decimal
    estimated_fees: Decimal
    slippage_reserve: Decimal
    settlement_cost: Decimal
    total_cost: Decimal
    assumed_payout: Decimal
    net_profit: Decimal
    net_edge_bps: Decimal
    roi_bps: Decimal
    book_age_ms: float
    pair_skew_ms: float
    fee_rate: Decimal
    seconds_delay: int
    qualifying: bool
    rejection_reasons: tuple[str, ...]
    snapshot_ts: float = 0.0


@dataclass(frozen=True, slots=True)
class Evaluation:
    market_id: str
    status: str
    reason: str | None = None
    opportunity: Opportunity | None = None


@dataclass(frozen=True, slots=True)
class _Economics:
    shares: Decimal
    notional_0: Decimal
    notional_1: Decimal
    fees: Decimal
    slippage: Decimal
    settlement: Decimal
    total_cost: Decimal
    net_profit: Decimal


class SameMarketDetector:
    def __init__(self, config: DetectorConfig) -> None:
        self.config = config
        if config.min_shares <= ZERO or config.max_shares < config.min_shares:
            raise ValueError("invalid share limits")
        if config.max_capital <= ZERO:
            raise ValueError("max_capital must be positive")
        if config.slippage_bps < ZERO or config.settlement_cost_per_pair < ZERO:
            raise ValueError("cost reserves cannot be negative")

    def evaluate(
        self,
        market: SportsMarket,
        cache: OrderBookCache,
        *,
        now: float | None = None,
    ) -> Evaluation:
        now = time.time() if now is None else now
        book_0 = cache.get(market.asset_ids[0])
        book_1 = cache.get(market.asset_ids[1])
        if book_0 is None or book_1 is None:
            return Evaluation(market.market_id, "incomplete_book", "missing one or both books")
        if book_0.condition_id != market.condition_id or book_1.condition_id != market.condition_id:
            return Evaluation(market.market_id, "invalid_book", "condition ID mismatch")
        if book_0.best_ask is None or book_1.best_ask is None:
            return Evaluation(
                market.market_id, "incomplete_book", "one or both ask sides are empty"
            )

        book_age_ms = max(book_0.age_ms(now), book_1.age_ms(now))
        if book_age_ms > self.config.max_book_age_ms:
            return Evaluation(
                market.market_id,
                "stale_book",
                f"book age {book_age_ms:.0f}ms exceeds limit",
            )

        if book_0.source_timestamp is not None and book_1.source_timestamp is not None:
            pair_skew_ms = abs(book_0.source_timestamp - book_1.source_timestamp) * 1000
        else:
            pair_skew_ms = abs(book_0.received_at - book_1.received_at) * 1000
        if pair_skew_ms > self.config.max_pair_skew_ms:
            return Evaluation(
                market.market_id,
                "skewed_book",
                f"pair timestamp skew {pair_skew_ms:.0f}ms exceeds limit",
            )

        effective_min = max(
            self.config.min_shares,
            market.order_min_size,
            book_0.min_order_size,
            book_1.min_order_size,
        )
        depth = min(_ask_depth(book_0.asks), _ask_depth(book_1.asks))
        hard_max = min(self.config.max_shares, depth)
        if hard_max < effective_min:
            return Evaluation(
                market.market_id,
                "insufficient_depth",
                f"paired ask depth {hard_max} is below minimum {effective_min}",
            )

        candidates = {effective_min, hard_max}
        for boundary in _depth_boundaries(book_0.asks) | _depth_boundaries(book_1.asks):
            if effective_min < boundary < hard_max:
                candidates.add(boundary)

        hard_max_cost = self._economics(market, book_0.asks, book_1.asks, hard_max).total_cost
        if hard_max_cost > self.config.max_capital:
            # Add the exact approximate capital boundary, not just lower discrete
            # book levels. A final execution engine would apply lot precision.
            capital_size = self._capital_boundary(
                market, book_0.asks, book_1.asks, effective_min, hard_max
            )
            if capital_size is not None:
                candidates.add(capital_size)

        if self.config.whole_contracts:
            candidates = {size.to_integral_value(rounding=ROUND_DOWN) for size in candidates}
        economics = {
            size: self._economics(market, book_0.asks, book_1.asks, size) for size in candidates
        }

        feasible = [
            economics[size]
            for size in candidates
            if size >= effective_min and economics[size].total_cost <= self.config.max_capital
        ]
        if not feasible:
            return Evaluation(
                market.market_id, "capital_limit", "minimum size exceeds capital limit"
            )

        best = max(feasible, key=lambda item: (item.net_profit, -item.total_cost))
        rejection_reasons: list[str] = []
        if best.net_profit < self.config.min_net_profit:
            rejection_reasons.append("below_min_net_profit")
        net_edge_bps = best.net_profit / best.shares * BPS
        if net_edge_bps < self.config.min_net_edge_bps:
            rejection_reasons.append("below_min_net_edge")

        best_ask_sum = book_0.best_ask.price + book_1.best_ask.price
        roi_bps = best.net_profit / best.total_cost * BPS if best.total_cost > ZERO else ZERO
        opportunity = Opportunity(
            market_id=market.market_id,
            condition_id=market.condition_id,
            question=market.question,
            sports_market_type=market.sports_market_type,
            outcome_labels=market.outcome_labels,
            asset_ids=market.asset_ids,
            shares=best.shares,
            best_ask_sum=best_ask_sum,
            gross_top_edge_bps=(ONE - best_ask_sum) * BPS,
            outcome_0_notional=best.notional_0,
            outcome_1_notional=best.notional_1,
            estimated_fees=best.fees,
            slippage_reserve=best.slippage,
            settlement_cost=best.settlement,
            total_cost=best.total_cost,
            assumed_payout=best.shares,
            net_profit=best.net_profit,
            net_edge_bps=net_edge_bps,
            roi_bps=roi_bps,
            book_age_ms=book_age_ms,
            pair_skew_ms=pair_skew_ms,
            fee_rate=market.fee_schedule.rate,
            seconds_delay=market.seconds_delay,
            qualifying=not rejection_reasons,
            rejection_reasons=tuple(rejection_reasons),
            snapshot_ts=now,
        )
        return Evaluation(
            market.market_id,
            "qualifying" if not rejection_reasons else "below_threshold",
            opportunity=opportunity,
        )

    def _economics(
        self,
        market: SportsMarket,
        asks_0: tuple[BookLevel, ...],
        asks_1: tuple[BookLevel, ...],
        shares: Decimal,
    ) -> _Economics:
        notional_0, fees_0 = self._walk_asks(asks_0, shares, market.fee_schedule)
        notional_1, fees_1 = self._walk_asks(asks_1, shares, market.fee_schedule)
        slippage = (notional_0 + notional_1) * self.config.slippage_bps / BPS
        settlement = shares * self.config.settlement_cost_per_pair
        fees_0 += self.config.per_order_cost_reserve
        fees_1 += self.config.per_order_cost_reserve
        total_cost = notional_0 + notional_1 + fees_0 + fees_1 + slippage + settlement
        return _Economics(
            shares=shares,
            notional_0=notional_0,
            notional_1=notional_1,
            fees=fees_0 + fees_1,
            slippage=slippage,
            settlement=settlement,
            total_cost=total_cost,
            net_profit=shares - total_cost,
        )

    def _capital_boundary(
        self,
        market: SportsMarket,
        asks_0: tuple[BookLevel, ...],
        asks_1: tuple[BookLevel, ...],
        low: Decimal,
        high: Decimal,
    ) -> Decimal | None:
        minimum = low
        if self._economics(market, asks_0, asks_1, low).total_cost > self.config.max_capital:
            return None
        # Costs are monotone as more ask depth is consumed. Decimal bisection
        # avoids float oversizing at the capital boundary.
        for _ in range(48):
            middle = (low + high) / 2
            cost = self._economics(market, asks_0, asks_1, middle).total_cost
            if cost <= self.config.max_capital:
                low = middle
            else:
                high = middle
        # Conservative research precision only; an execution engine must derive
        # actual amount precision and rounding rules from the current venue SDK.
        quantized = low.quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
        return quantized if quantized >= minimum else None

    @staticmethod
    def _walk_asks(
        asks: tuple[BookLevel, ...], shares: Decimal, fee_schedule: FeeSchedule
    ) -> tuple[Decimal, Decimal]:
        remaining = shares
        notional = ZERO
        fees = ZERO
        for level in asks:
            if remaining == ZERO:
                break
            filled = min(remaining, level.size)
            notional += filled * level.price
            fees += fee_schedule.fee(filled, level.price)
            remaining -= filled
        if remaining > ZERO:
            raise ValueError("ask walk exceeds validated depth")
        return notional, fees


def _ask_depth(asks: tuple[BookLevel, ...]) -> Decimal:
    return sum((level.size for level in asks), ZERO)


def _depth_boundaries(asks: tuple[BookLevel, ...]) -> set[Decimal]:
    result: set[Decimal] = set()
    cumulative = ZERO
    for level in asks:
        cumulative += level.size
        result.add(cumulative)
    return result
