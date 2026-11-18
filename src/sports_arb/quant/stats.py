"""Probability scoring, Bayesian updating and uncertainty. Pure Python, auditable."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

EPS = 1e-6


def clip(p: float) -> float:
    return min(1 - EPS, max(EPS, p))


def logit(p: float) -> float:
    p = clip(p)
    return math.log(p / (1 - p))


def sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x)) if x >= 0 else math.exp(x) / (1 + math.exp(x))


def brier(probs: Sequence[float], outcomes: Sequence[int]) -> float:
    return sum((p - y) ** 2 for p, y in zip(probs, outcomes, strict=True)) / len(probs)


def log_loss(probs: Sequence[float], outcomes: Sequence[int]) -> float:
    return -sum(
        y * math.log(clip(p)) + (1 - y) * math.log(1 - clip(p))
        for p, y in zip(probs, outcomes, strict=True)
    ) / len(probs)


def reliability(probs: Sequence[float], outcomes: Sequence[int], bins: int = 10):
    """(mean predicted, observed frequency, n) per non-empty bin, and expected calibration error."""
    buckets: list[list[tuple[float, int]]] = [[] for _ in range(bins)]
    for p, y in zip(probs, outcomes, strict=True):
        buckets[min(bins - 1, int(p * bins))].append((p, y))
    rows, ece, total = [], 0.0, len(probs)
    for b in buckets:
        if not b:
            continue
        mp = sum(p for p, _ in b) / len(b)
        fr = sum(y for _, y in b) / len(b)
        rows.append((mp, fr, len(b)))
        ece += len(b) / total * abs(mp - fr)
    return rows, ece


@dataclass(frozen=True, slots=True)
class BetaPosterior:
    """Conjugate Beta-Bernoulli belief about an unknown probability (Bayesian updating)."""

    alpha: float = 1.0
    beta: float = 1.0

    def update(self, successes: int, failures: int) -> BetaPosterior:
        return BetaPosterior(self.alpha + successes, self.beta + failures)

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @property
    def sd(self) -> float:
        n = self.alpha + self.beta
        return math.sqrt(self.alpha * self.beta / (n * n * (n + 1)))

    def lower(self, z: float = 1.645) -> float:
        """Normal-approximation lower credible bound (~95% one-sided). Conservative: floor 0."""
        return max(0.0, self.mean - z * self.sd)


def wilson_interval(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 1.0
    p = wins / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, centre - half), min(1.0, centre + half)


def paired_bootstrap_gain(
    base_losses: Sequence[float],
    model_losses: Sequence[float],
    *,
    rounds: int = 2000,
    seed: int = 11,
) -> tuple[float, float]:
    """Mean per-sample loss improvement (baseline - model) and its 5th percentile.

    The 5th percentile > 0 is the bar for 'the model beats the baseline out of sample'.
    """
    import random

    rng = random.Random(seed)
    diffs = [b - m for b, m in zip(base_losses, model_losses, strict=True)]
    n = len(diffs)
    means = sorted(sum(diffs[rng.randrange(n)] for _ in range(n)) / n for _ in range(rounds))
    return sum(diffs) / n, means[int(0.05 * rounds)]
