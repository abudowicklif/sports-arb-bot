from decimal import Decimal as D

import pytest

from sports_arb.accounting import CapitalAccount
from sports_arb.quant.allocation import strategy_weights
from sports_arb.quant.decision import Candidate, DecisionEngine, Limits
from sports_arb.quant.equivalence import EquivalenceError, load_equivalences
from sports_arb.quant.registry import AUTH, ValidationRegistry
from sports_arb.quant.sportsdata import SignalGate, SportsSignal


def reg(tmp_path, approved=()):
    r = ValidationRegistry(tmp_path / "reg.json")
    for name in approved:
        r.record_evidence(name, {"report": "x"})
        r.approve(name, AUTH)
    return r


def cand(
    strategy="pure_arb", event="e1", group="g1", venue="polymarket", capital="5", ev="0.5", **kw
):
    base = dict(
        strategy=strategy,
        event_key=event,
        group_key=group,
        venue=venue,
        capital=D(capital),
        ev_lower=D(ev),
        liquidity_ok=True,
        execution_ok=True,
        data_ok=True,
        win_prob_lower=0.6,
    )
    base.update(kw)
    return Candidate(**base)


def engine(tmp_path, approved=("pure_arb", "stat_arb", "hybrid"), **limits):
    return DecisionEngine(reg(tmp_path, approved), Limits(**limits), available_capital=D("100"))


def test_everything_is_rejected_until_a_human_approves_the_strategy(tmp_path):
    d = engine(tmp_path, approved=()).decide([cand(), cand("stat_arb", event="e2")])
    assert d.selected == []
    assert all("strategy_unvalidated" in r for _, r in d.rejected)


def test_registry_rules(tmp_path):
    r = reg(tmp_path)
    with pytest.raises(PermissionError):
        r.approve("stat_arb", AUTH)  # no evidence
    r.record_evidence("stat_arb", {"x": 1})
    with pytest.raises(PermissionError):
        r.approve("stat_arb", "pls")
    r.approve("stat_arb", AUTH)
    assert r.approved("stat_arb")
    r.record_evidence("stat_arb", {"x": 2})  # new evidence => re-approval needed
    assert not r.approved("stat_arb")
    r.suspend("pure_arb", "drift")
    assert r.status("pure_arb") == "SUSPENDED"
    assert ValidationRegistry(tmp_path / "reg.json").status("pure_arb") == "SUSPENDED"


def test_profitable_looking_but_unreliable_opportunities_are_rejected(tmp_path):
    e = engine(tmp_path)
    cases = {
        "lower_bound_ev_not_positive": cand(ev="0"),
        "insufficient_liquidity": cand(liquidity_ok=False),
        "execution_not_feasible": cand(execution_ok=False),
        "data_quality": cand(data_ok=False),
        "model_not_reliable": cand("stat_arb", model_ok=False),
        "missing_probability_uncertainty": cand("stat_arb", win_prob_lower=None),
        "ev_per_capital_below_minimum": cand(capital="10", ev="0.01"),
    }
    for reason, c in cases.items():
        d = e.decide([c])
        assert d.selected == [] and reason in d.rejected[0][1], reason


def test_strategies_cannot_duplicate_exposure_on_one_event_or_game(tmp_path):
    e = engine(tmp_path, per_event=D("6"), per_group=D("8"))
    d = e.decide(
        [
            cand("pure_arb", ev="1.0"),
            cand("stat_arb", ev="0.8"),  # same event, other strategy
            cand("hybrid", event="e2", ev="0.7", capital="5"),
        ]
    )  # same game, over group cap
    assert [c.strategy for c in d.selected] == ["pure_arb"]
    reasons = {c.strategy: r for c, r in d.rejected}
    assert "event_exposure_limit" in reasons["stat_arb"]
    assert "correlated_group_limit" in reasons["hybrid"]


def test_capital_concurrency_and_ranking(tmp_path):
    e = DecisionEngine(
        reg(tmp_path, ("pure_arb",)), Limits(max_concurrent=2), available_capital=D("100")
    )
    cs = [cand(event=f"e{i}", group=f"g{i}", ev=str(0.2 + i / 10)) for i in range(4)]
    d = e.decide(cs)
    assert [c.event_key for c in d.selected] == ["e3", "e2"]  # best EV/capital first
    assert any("concurrency_limit" in r for _, r in d.rejected)
    tiny = DecisionEngine(reg(tmp_path, ("pure_arb",)), available_capital=D("3"))
    assert "capital_limit" in tiny.decide([cand()]).rejected[0][1]


def test_equivalence_requires_reviewed_mapping(tmp_path):
    good = tmp_path / "m.toml"
    good.write_text("""
[[pair]]
event_key="nba-2026-11-11-lal-bos"
polymarket_condition_id="0xabc"
kalshi_ticker="KXNBA-26NOV11LALBOS-LAL"
settlement_equivalent=true
reviewed_by="reytrf"
reviewed_on="2026-11-11"
[pair.outcome_map]
"Lakers"="YES"
"Celtics"="NO"
[[pair]]
event_key="not-same"
polymarket_condition_id="0xdef"
kalshi_ticker="K2"
settlement_equivalent=false
reviewed_by="reytrf"
reviewed_on="2026-11-11"
[pair.outcome_map]
"A"="YES"
"B"="NO"
""")
    pairs = load_equivalences(good)
    assert list(pairs) == ["nba-2026-11-11-lal-bos"]  # non-equivalent skipped
    bad = tmp_path / "b.toml"
    bad.write_text('[[pair]]\nevent_key="x"\n')
    with pytest.raises(EquivalenceError):
        load_equivalences(bad)
    assert load_equivalences(tmp_path / "missing.toml") == {}  # default: nothing comparable


def sig(kind, value, source, rx, ev=None):
    return SportsSignal("g1", kind, value, source, ev if ev is not None else rx - 0.5, rx)


def test_signal_gate():
    g = SignalGate(max_age_s=5, min_sources=2)
    ok = [sig("score", "1-0", "a", 100), sig("score", "1-0", "b", 100)]
    assert g.assess(ok, "g1", 101).ok
    assert g.assess([], "g1", 101).reason == "no_data"
    assert g.assess(ok, "g1", 110).reason == "stale_signal"
    assert g.assess(ok[:1], "g1", 101).reason == "insufficient_independent_sources"
    conflict = [sig("score", "1-0", "a", 100), sig("score", "0-0", "b", 100)]
    assert g.assess(conflict, "g1", 101).reason == "conflicting_sources"
    lag = [sig("score", "1-0", "a", 100, ev=90), sig("score", "1-0", "b", 100)]
    assert g.assess(lag, "g1", 101).reason == "source_lag_too_high"


def test_allocation_only_to_approved_strategies_with_proven_realized_results(tmp_path):
    r = reg(tmp_path, ("pure_arb",))
    good = [D("0.10"), D("0.12"), D("0.09")] * 12
    weights = strategy_weights({"pure_arb": good, "stat_arb": good}, r)
    assert weights["pure_arb"] > 0 and weights["stat_arb"] == 0  # stat_arb unapproved
    assert strategy_weights({"pure_arb": good[:5]}, r)["pure_arb"] == 0  # too few trades
    mixed = [D("0.5"), D("-0.6")] * 20
    assert strategy_weights({"pure_arb": mixed}, r)["pure_arb"] == 0  # no positive lower bound


def test_capital_account_tracks_pnl_per_strategy(tmp_path):
    a = CapitalAccount(tmp_path / "c.db")
    a.deposit(D("100"), "d")
    a.recognize_pnl("r1", D("1"), {"strategy": "pure_arb"})
    a.recognize_pnl("r2", D("-2"), {"strategy": "stat_arb"})
    assert a.pnl_by_strategy() == {"pure_arb": [D("1")], "stat_arb": [D("-2")]}
