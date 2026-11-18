"""Central decision engine across pure_arb, stat_arb and hybrid.

Rejects by default. A candidate is selected only if its strategy is approved, data and
execution conditions are good, the LOWER-BOUND expected value is positive, and exposure
limits hold. Strategies stay separately measurable (every candidate carries its tag) and
cannot silently stack risk on the same event or game.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from sports_arb.quant.registry import STRATEGIES, ValidationRegistry

ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class Candidate:
    strategy: str
    event_key: str  # one real-world outcome (all venues/markets for it)
    group_key: str  # the game: correlated markets share it
    venue: str
    capital: Decimal  # cost incl. fees
    ev_lower: Decimal  # lower-bound expected NET profit, total dollars
    liquidity_ok: bool
    execution_ok: bool  # latency/health/rules verified
    data_ok: bool  # sports-data & book quality
    model_ok: bool = True  # model not suspended / regime validated (stat/hybrid)
    sport: str = "unknown"
    win_prob_lower: float | None = None
    detail: dict = field(default_factory=dict)


@dataclass(slots=True)
class Limits:
    per_event: Decimal = Decimal("10")
    per_group: Decimal = Decimal("15")
    per_venue: Decimal = Decimal("30")
    per_strategy: Decimal = Decimal("20")
    per_sport: Decimal = Decimal("25")
    total: Decimal = Decimal("40")
    max_concurrent: int = 4
    min_ev_per_capital: Decimal = Decimal("0.005")


@dataclass(slots=True)
class Decision:
    selected: list[Candidate] = field(default_factory=list)
    rejected: list[tuple[Candidate, list[str]]] = field(default_factory=list)


class ExposureBook:
    def __init__(self) -> None:
        self.by: dict[str, dict[str, Decimal]] = {
            k: defaultdict(Decimal) for k in ("event", "group", "venue", "strategy", "sport")
        }
        self.total, self.count = ZERO, 0

    def add(self, c: Candidate) -> None:
        for dim, key in (
            ("event", c.event_key),
            ("group", c.group_key),
            ("venue", c.venue),
            ("strategy", c.strategy),
            ("sport", c.sport),
        ):
            self.by[dim][key] += c.capital
        self.total += c.capital
        self.count += 1


class DecisionEngine:
    def __init__(
        self,
        registry: ValidationRegistry,
        limits: Limits | None = None,
        available_capital: Decimal = ZERO,
    ) -> None:
        self.registry = registry
        self.limits = limits or Limits()
        self.available = available_capital

    def _reasons(self, c: Candidate, book: ExposureBook) -> list[str]:
        L, r = self.limits, []
        if c.strategy not in STRATEGIES:
            return ["unknown_strategy"]
        if not self.registry.approved(c.strategy):
            r.append(f"strategy_{self.registry.status(c.strategy).lower()}")
        if not c.data_ok:
            r.append("data_quality")
        if not c.execution_ok:
            r.append("execution_not_feasible")
        if not c.liquidity_ok:
            r.append("insufficient_liquidity")
        if not c.model_ok:
            r.append("model_not_reliable")
        if c.capital <= ZERO:
            r.append("no_capital_required_invalid")
        elif c.ev_lower <= ZERO:
            r.append("lower_bound_ev_not_positive")
        elif c.ev_lower / c.capital < L.min_ev_per_capital:
            r.append("ev_per_capital_below_minimum")
        if c.strategy in {"stat_arb", "hybrid"} and c.win_prob_lower is None:
            r.append("missing_probability_uncertainty")
        if book.by["event"][c.event_key] + c.capital > L.per_event:
            r.append("event_exposure_limit")
        if book.by["group"][c.group_key] + c.capital > L.per_group:
            r.append("correlated_group_limit")
        if book.by["venue"][c.venue] + c.capital > L.per_venue:
            r.append("venue_limit")
        if book.by["strategy"][c.strategy] + c.capital > L.per_strategy:
            r.append("strategy_limit")
        if book.by["sport"][c.sport] + c.capital > L.per_sport:
            r.append("sport_exposure_limit")
        if book.total + c.capital > min(L.total, self.available):
            r.append("capital_limit")
        if book.count >= L.max_concurrent:
            r.append("concurrency_limit")
        return r

    def decide(self, candidates: list[Candidate], existing: ExposureBook | None = None) -> Decision:
        book, out = existing or ExposureBook(), Decision()
        ranked = sorted(
            candidates,
            key=lambda c: (c.ev_lower / c.capital) if c.capital > ZERO else Decimal("-1"),
            reverse=True,
        )
        for c in ranked:
            reasons = self._reasons(c, book)
            if reasons:
                out.rejected.append((c, reasons))
            else:
                book.add(c)
                out.selected.append(c)
        return out
