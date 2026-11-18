"""Capital allocation across strategies from independently demonstrated REALIZED results."""

from __future__ import annotations

import math
from decimal import Decimal

from sports_arb.quant.registry import STRATEGIES, ValidationRegistry


def strategy_weights(
    pnl_by_strategy: dict[str, list[Decimal]],
    registry: ValidationRegistry,
    *,
    min_trades: int = 30,
    max_weight: float = 0.6,
) -> dict[str, float]:
    """Weight ~ lower confidence bound of mean realized PnL per trade.

    A strategy gets capital only if approved, has >= min_trades realized trades, and a
    positive lower bound. Weights are capped; the unallocated remainder stays in reserve
    (cash), never redistributed to unproven strategies.
    """
    scores: dict[str, float] = {}
    for name in STRATEGIES:
        xs = [float(x) for x in pnl_by_strategy.get(name, [])]
        if not registry.approved(name) or len(xs) < min_trades:
            continue
        mean = sum(xs) / len(xs)
        sd = math.sqrt(sum((x - mean) ** 2 for x in xs) / (len(xs) - 1))
        lower = mean - 1.645 * sd / math.sqrt(len(xs))
        if lower > 0:
            scores[name] = lower
    total = sum(scores.values())
    if total <= 0:
        return {name: 0.0 for name in STRATEGIES}
    raw = {name: scores.get(name, 0.0) / total for name in STRATEGIES}
    return {name: min(max_weight, w) for name, w in raw.items()}
