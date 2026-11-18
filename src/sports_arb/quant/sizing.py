"""Conservative Kelly sizing for buying a binary contract. Never sizes on a point estimate."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class SizingResult:
    stake: Decimal  # dollars to spend (cost incl. fees)
    shares: Decimal
    kelly_full: float
    reason: str


def kelly_stake(
    *,
    win_prob_lower: float,
    price: Decimal,
    fee_per_share: Decimal,
    bankroll: Decimal,
    kelly_multiplier: float = 0.25,
    max_fraction: float = 0.05,
    max_stake: Decimal | None = None,
) -> SizingResult:
    """Fractional Kelly using the LOWER credible bound of the win probability.

    k = all-in cost per $1 payoff. Full Kelly stake fraction = (q - k) / (1 - k). We use
    q = lower bound, a fraction of Kelly, and hard caps. Non-positive edge at the lower
    bound means no trade: a high point estimate never authorizes a position on its own.
    """
    k = price + fee_per_share
    if not Decimal("0") < k < Decimal("1"):
        return SizingResult(Decimal(0), Decimal(0), 0.0, "invalid_cost")
    full = (win_prob_lower - float(k)) / (1 - float(k))
    if full <= 0:
        return SizingResult(Decimal(0), Decimal(0), full, "no_edge_at_lower_bound")
    fraction = min(full * kelly_multiplier, max_fraction)
    stake = (bankroll * Decimal(str(fraction))).quantize(Decimal("0.000001"))
    if max_stake is not None:
        stake = min(stake, max_stake)
    shares = (stake / k).quantize(Decimal("0.01"))
    if shares <= 0:
        return SizingResult(Decimal(0), Decimal(0), full, "below_minimum")
    return SizingResult(stake, shares, full, "ok")
