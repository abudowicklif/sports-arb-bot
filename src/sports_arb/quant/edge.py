"""Edge discovery: pre-registered hypotheses and an event-study test for delayed reactions.

An edge is enabled only if its pre-declared validation requirements pass on unseen data,
net of costs. This module supplies the statistics; the data must come from real recordings.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Hypothesis:
    name: str
    statement: str  # the mathematical hypothesis
    rationale: str  # why the inefficiency might exist
    required_data: str
    min_events: int = 200  # predefined validation requirements
    min_net_gain: float = 0.0  # per-event, after costs
    require_p05_positive: bool = True


HYPOTHESES = (
    Hypothesis(
        "slow_reaction_after_score",
        "E[p(t+h) - p(t0+d)] != 0 after a score change, for execution delay d and horizon h",
        "Thin books and delayed feeds may adjust to goals/points over seconds, not instantly",
        "Timestamped score events from a licensed feed + order-book snapshots around them",
    ),
    Hypothesis(
        "overreaction_after_red_card",
        "E[p(t+h) - p(t0+d)] has the OPPOSITE sign of the initial move after red cards",
        "Traders may overweight dramatic events; prices may partially revert",
        "Red-card events + books around them",
    ),
    Hypothesis(
        "favourite_longshot_bias",
        "P(win | market p) != p, i.e. logistic recalibration slope b != 1 or intercept a != 0",
        "Documented in betting markets; may persist for prediction-market longshots",
        "Resolved markets with price at decision time",
    ),
)


@dataclass(slots=True)
class EventStudyResult:
    n: int
    mean_net: float
    p05: float
    p95: float
    significant: bool
    reason: str


def event_study(
    price_at_decision: Sequence[float],
    price_at_exit: Sequence[float],
    direction: Sequence[int],
    cost_per_unit: float,
    *,
    min_events: int = 200,
    rounds: int = 3000,
    seed: int = 5,
) -> EventStudyResult:
    """Net gain per event of trading in the hypothesized ``direction`` (+1 buy, -1 sell) from
    the price available AFTER your execution delay to the exit price, minus costs.

    Significant only if n >= min_events and the 5th percentile of the bootstrap mean is > 0.
    """
    gains = [
        d * (exit_ - entry) - cost_per_unit
        for entry, exit_, d in zip(price_at_decision, price_at_exit, direction, strict=True)
    ]
    n = len(gains)
    if n < min_events:
        return EventStudyResult(
            n,
            sum(gains) / n if n else 0.0,
            0.0,
            0.0,
            False,
            f"insufficient events ({n} < {min_events})",
        )
    rng = random.Random(seed)
    means = sorted(sum(gains[rng.randrange(n)] for _ in range(n)) / n for _ in range(rounds))
    p05, p95 = means[int(0.05 * rounds)], means[int(0.95 * rounds)]
    mean = sum(gains) / n
    ok = p05 > 0
    return EventStudyResult(
        n,
        mean,
        p05,
        p95,
        ok,
        "net-of-cost gain is positive with confidence"
        if ok
        else "not distinguishable from zero after costs",
    )
