import math
import random
from decimal import Decimal as D

import pytest

from sports_arb.quant.drift import DriftMonitor
from sports_arb.quant.models import (
    Ensemble,
    LogisticRecalibrator,
    MarketBaseline,
    Observation,
    regime,
    walk_forward,
)
from sports_arb.quant.sizing import kelly_stake
from sports_arb.quant.stats import (
    BetaPosterior,
    brier,
    log_loss,
    logit,
    reliability,
    sigmoid,
    wilson_interval,
)


def test_scores_known_values():
    assert brier([0.5, 0.5], [1, 0]) == 0.25
    assert math.isclose(log_loss([0.5, 0.5], [1, 0]), math.log(2))
    _, ece = reliability([0.1] * 10 + [0.9] * 10, [0] * 9 + [1] + [1] * 9 + [0])
    assert ece < 1e-9  # perfectly calibrated bins


def test_bayesian_update_tightens_and_lower_bound_is_conservative():
    prior = BetaPosterior()
    small, large = prior.update(6, 4), prior.update(600, 400)
    assert large.sd < small.sd
    assert small.lower() < small.mean and 0 <= small.lower()
    lo, hi = wilson_interval(60, 100)
    assert lo < 0.6 < hi


def test_kelly_uses_lower_bound_and_never_trades_without_edge():
    none = kelly_stake(
        win_prob_lower=0.50, price=D("0.55"), fee_per_share=D("0.01"), bankroll=D("100")
    )
    assert none.stake == 0 and none.reason == "no_edge_at_lower_bound"
    ok = kelly_stake(
        win_prob_lower=0.70, price=D("0.55"), fee_per_share=D("0.01"), bankroll=D("100")
    )
    assert ok.reason == "ok" and 0 < ok.stake <= D("5")  # capped at 5% of bankroll
    assert (
        kelly_stake(
            win_prob_lower=0.9, price=D("1.2"), fee_per_share=D("0"), bankroll=D("100")
        ).reason
        == "invalid_cost"
    )


def synth(n, *, edge, seed):
    """True probability = market probability, distorted only if edge=True (planted bias)."""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        p_mkt = rng.uniform(0.1, 0.9)
        p_true = sigmoid(0.7 * logit(p_mkt) + 0.2) if edge else p_mkt
        out.append(Observation(float(i), p_mkt, 1 if rng.random() < p_true else 0))
    return out


def test_walk_forward_approves_planted_bias_only_with_evidence():
    r = walk_forward(synth(3000, edge=True, seed=1), LogisticRecalibrator)
    assert r.approved and r.gain_p05 > 0


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_walk_forward_does_not_approve_when_market_is_already_calibrated(seed):
    r = walk_forward(synth(3000, edge=False, seed=seed), LogisticRecalibrator)
    assert not r.approved  # false-positive control


def test_walk_forward_refuses_thin_data():
    r = walk_forward(synth(250, edge=True, seed=1), LogisticRecalibrator)
    assert not r.approved and "insufficient" in r.reason


def test_walk_forward_never_trains_on_the_future():
    seen = []

    class Spy(MarketBaseline):
        def fit(self, data):
            seen.append(max(o.ts for o in data))
            self.max_train = seen[-1]
            return self

        def predict(self, o):
            assert o.ts > self.max_train, "test point is not strictly after training data"
            return super().predict(o)

    walk_forward(synth(800, edge=False, seed=5), Spy, min_oos=100)
    assert seen == sorted(seen) and len(seen) > 1


def test_recalibrator_defaults_to_market_with_no_data_and_ensemble_blends():
    m = LogisticRecalibrator().fit([])
    o = Observation(0, 0.62, 1)
    assert math.isclose(m.predict(o), 0.62, abs_tol=1e-6)
    e = Ensemble([MarketBaseline(), LogisticRecalibrator()]).fit([])
    assert math.isclose(e.predict(o), 0.62, abs_tol=1e-6)


def test_regime_detection():
    calm = [0.5 + 0.001 * (i % 2) for i in range(40)]
    wild = [0.3 if i % 2 else 0.7 for i in range(40)]
    assert regime(calm) == "calm" and regime(wild) == "turbulent"


def test_drift_suspends_model_and_needs_authorization_to_resume():
    d = DriftMonitor(min_samples=20)
    for _ in range(30):
        d.record(baseline_loss=0.60, model_loss=0.62)  # model worse than the market
    assert d.suspended
    with pytest.raises(PermissionError):
        d.reapprove("yes")
    d.reapprove("AUTHORIZE MODEL RESUME")
    assert not d.suspended
