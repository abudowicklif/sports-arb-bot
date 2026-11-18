"""Probability models, ensemble, regime detection and walk-forward validation.

The market-implied probability is the BASELINE every model must beat out of sample.
Nothing is trusted until ``walk_forward`` approves it on resolved real markets.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from sports_arb.quant.stats import (
    brier,
    clip,
    log_loss,
    logit,
    paired_bootstrap_gain,
    sigmoid,
)


@dataclass(frozen=True, slots=True)
class Observation:
    ts: float
    market_prob: float  # market-implied probability of the outcome at decision time
    outcome: int  # 1 if the outcome resolved true
    features: dict[str, float] = field(default_factory=dict)


class MarketBaseline:
    """The null model: the price is right."""

    name = "market_baseline"

    def fit(self, data: Sequence[Observation]) -> MarketBaseline:
        return self

    def predict(self, o: Observation) -> float:
        return clip(o.market_prob)


class LogisticRecalibrator:
    """logit(p) = a + b*logit(market). Ridge-shrunk toward (a=0, b=1), i.e. toward the market.

    Captures favourite/longshot bias or systematic over/under-confidence and nothing more.
    With little data the penalty keeps it equal to the baseline.
    """

    name = "logistic_recalibrator"

    def __init__(self, ridge: float = 20.0) -> None:
        self.ridge, self.a, self.b = ridge, 0.0, 1.0

    def fit(self, data: Sequence[Observation]) -> LogisticRecalibrator:
        a, b = 0.0, 1.0
        xs = [logit(o.market_prob) for o in data]
        ys = [o.outcome for o in data]
        for _ in range(50):
            ga = self.ridge * a
            gb = self.ridge * (b - 1.0)
            haa, hab, hbb = self.ridge, 0.0, self.ridge
            for x, y in zip(xs, ys, strict=True):
                p = sigmoid(a + b * x)
                w = p * (1 - p)
                ga += p - y
                gb += (p - y) * x
                haa += w
                hab += w * x
                hbb += w * x * x
            det = haa * hbb - hab * hab
            if abs(det) < 1e-12:
                break
            da = (hbb * ga - hab * gb) / det
            db = (haa * gb - hab * ga) / det
            a, b = a - da, b - db
            if abs(da) + abs(db) < 1e-9:
                break
        self.a, self.b = a, b
        return self

    def predict(self, o: Observation) -> float:
        return clip(sigmoid(self.a + self.b * logit(o.market_prob)))


class Ensemble:
    """Logit-space weighted blend. Weights ~ 1/out-of-sample log loss of each member."""

    name = "ensemble"

    def __init__(self, members: Sequence, weights: Sequence[float] | None = None) -> None:
        self.members = list(members)
        self.weights = list(weights) if weights else [1.0] * len(self.members)

    def fit(self, data: Sequence[Observation]) -> Ensemble:
        for m in self.members:
            m.fit(data)
        return self

    def predict(self, o: Observation) -> float:
        total = sum(self.weights)
        z = sum(w * logit(m.predict(o)) for m, w in zip(self.members, self.weights, strict=True))
        return clip(sigmoid(z / total))


def realized_volatility(prices: Sequence[float], window: int = 30) -> float:
    xs = [logit(p) for p in prices[-(window + 1) :]]
    if len(xs) < 3:
        return 0.0
    d = [b - a for a, b in zip(xs, xs[1:], strict=False)]
    mean = sum(d) / len(d)
    return math.sqrt(sum((x - mean) ** 2 for x in d) / (len(d) - 1))


def regime(prices: Sequence[float], *, threshold: float = 0.15, window: int = 30) -> str:
    """'calm' vs 'turbulent' from realized logit-price volatility. Models are validated and
    selected per regime; an unvalidated regime means no trading, not a default model."""
    return "turbulent" if realized_volatility(prices, window) >= threshold else "calm"


@dataclass(slots=True)
class WalkForwardReport:
    model: str
    folds: int
    n_oos: int
    brier_model: float
    brier_baseline: float
    logloss_model: float
    logloss_baseline: float
    mean_gain: float  # baseline log loss - model log loss (positive = better)
    gain_p05: float  # 5th percentile of the bootstrap of that gain
    approved: bool
    reason: str


def walk_forward(
    data: Sequence[Observation],
    make_model: Callable[[], object],
    *,
    min_train: int = 200,
    test_size: int = 50,
    min_oos: int = 300,
) -> WalkForwardReport:
    """Expanding-window walk-forward. Train strictly before test; no shuffling, no peeking.

    Approved only if: enough out-of-sample points AND the 5th percentile of the paired
    bootstrap log-loss gain over the market baseline is positive.
    """
    rows = sorted(data, key=lambda o: o.ts)
    probs, base, ys, folds = [], [], [], 0
    start = min_train
    while start + test_size <= len(rows):
        train, test = rows[:start], rows[start : start + test_size]
        model = make_model().fit(train)
        for o in test:
            probs.append(model.predict(o))
            base.append(clip(o.market_prob))
            ys.append(o.outcome)
        folds += 1
        start += test_size
    name = getattr(make_model(), "name", "model")
    if len(ys) < min_oos:
        return WalkForwardReport(
            name,
            folds,
            len(ys),
            0,
            0,
            0,
            0,
            0,
            0,
            False,
            f"insufficient out-of-sample data ({len(ys)} < {min_oos})",
        )
    ll_m = [-(y * math.log(p) + (1 - y) * math.log(1 - p)) for p, y in zip(probs, ys, strict=True)]
    ll_b = [-(y * math.log(p) + (1 - y) * math.log(1 - p)) for p, y in zip(base, ys, strict=True)]
    gain, p05 = paired_bootstrap_gain(ll_b, ll_m)
    approved = p05 > 0
    return WalkForwardReport(
        name,
        folds,
        len(ys),
        brier(probs, ys),
        brier(base, ys),
        log_loss(probs, ys),
        log_loss(base, ys),
        gain,
        p05,
        approved,
        "beats market baseline out of sample" if approved else "does not beat market baseline",
    )
