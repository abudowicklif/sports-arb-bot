import json
import random
from decimal import Decimal as D

import pytest

from sports_arb.accounting import CapitalAccount, CapitalPolicy
from sports_arb.dashboard import render_dashboard
from sports_arb.quant.decision import Candidate, DecisionEngine, Limits
from sports_arb.quant.edge import event_study
from sports_arb.quant.matchstate import MatchState, MatchTracker
from sports_arb.quant.registry import AUTH, ValidationRegistry
from sports_arb.quant.sportsdata import SignalGate, SportsSignal
from sports_arb.quant.staking import (
    RESET_PHRASE,
    StakeContext,
    StakingConfig,
    StakingEngine,
)


def reg(tmp_path, *names):
    r = ValidationRegistry(tmp_path / "r.json")
    for n in names:
        r.record_evidence(n, {"x": 1})
        r.approve(n, AUTH)
    return r


def ctx(**kw):
    base = dict(
        bankroll=D("100"),
        price=D("0.50"),
        win_prob_lower=0.60,
        edge_validated=True,
        central_cap=D("20"),
        sample_size=300,
    )
    base.update(kw)
    return StakeContext(**base)


def test_no_staking_method_bets_without_a_validated_edge(tmp_path):
    r = reg(
        tmp_path,
        *[f"staking:{m}" for m in ("martingale", "paroli", "adaptive", "capped_martingale")],
    )
    for m in ("flat", "kelly", "adaptive", "martingale", "capped_martingale", "paroli"):
        e = StakingEngine(StakingConfig(method=m), r)
        assert e.next_stake(ctx(edge_validated=False)).stake == 0


def test_progression_methods_need_registry_approval(tmp_path):
    e = StakingEngine(StakingConfig(method="martingale"), reg(tmp_path))
    d = e.next_stake(ctx())
    assert d.stake == 0 and d.reason == "staking_martingale_not_approved"
    ok = StakingEngine(StakingConfig(method="martingale"), reg(tmp_path, "staking:martingale"))
    assert ok.next_stake(ctx()).stake == D("1")


def test_martingale_progression_is_limited_and_never_unbounded(tmp_path):
    cfg = StakingConfig(
        method="martingale",
        max_steps=2,
        max_stake=D("100"),
        cumulative_loss_limit=D("1000"),
        daily_loss_limit=D("1000"),
    )
    e = StakingEngine(cfg, reg(tmp_path, "staking:martingale"))
    stakes = []
    for _ in range(6):
        d = e.next_stake(ctx(central_cap=D("1000"), bankroll=D("10000")))
        stakes.append(d.stake)
        if d.stake == 0:
            break
        e.record(False, -d.stake)
    assert stakes == [D("1"), D("2"), D("4"), D("0")] and e.halted
    assert e.next_stake(ctx()).reason == "halted_needs_reset"
    with pytest.raises(PermissionError):
        e.reset("pls")
    e.reset(RESET_PHRASE)
    assert not e.halted and e.step == 0


def test_central_risk_cap_always_wins_and_truncated_progression_stops(tmp_path):
    e = StakingEngine(
        StakingConfig(
            method="martingale", max_steps=5, cumulative_loss_limit=D("1000"), max_fraction=1.0
        ),
        reg(tmp_path, "staking:martingale"),
    )
    e.next_stake(ctx())
    e.record(False, D("-1"))
    e.record(False, D("-2"))
    d = e.next_stake(ctx(central_cap=D("3")))  # wants 4, cap is 3
    assert d.stake == 0 and d.reason == "risk_cap_truncates_progression"


def test_loss_limits_use_net_pnl_and_stop_staking(tmp_path):
    e = StakingEngine(StakingConfig(method="flat", daily_loss_limit=D("3")), None)
    for pnl in (D("-2"), D("+5"), D("-2")):  # net +1: must keep betting
        e.record(pnl > 0, pnl)
    assert e.next_stake(ctx()).stake > 0
    e.record(False, D("-4"))  # daily net -3
    assert e.next_stake(ctx()).reason == "daily_loss_limit"
    e.new_day()
    assert e.next_stake(ctx()).stake > 0


def test_paroli_presses_winners_only_and_resets_on_loss(tmp_path):
    e = StakingEngine(
        StakingConfig(method="paroli", paroli_wins=2, max_stake=D("50")),
        reg(tmp_path, "staking:paroli"),
    )
    seq = []
    for won in (True, True, True, False):
        d = e.next_stake(ctx())
        seq.append(d.stake)
        e.record(won, d.stake if won else -d.stake)
    assert seq == [D("1"), D("2"), D("4"), D("4")]
    assert e.next_stake(ctx()).stake == D("1")  # reset after loss


def test_kelly_refuses_when_lower_bound_edge_is_not_positive():
    e = StakingEngine(StakingConfig(method="kelly"), None)
    assert e.next_stake(ctx(win_prob_lower=0.49)).stake == 0
    assert e.next_stake(ctx(win_prob_lower=0.65)).stake > 0


def sig(value, source, rx=100.0):
    return SportsSignal("g1", "status", value, source, rx - 0.5, rx)


def tracker():
    return MatchTracker(
        SignalGate(max_age_s=5, min_sources=2),
        {
            "live": MatchState.IN_PROGRESS,
            "inplay": MatchState.IN_PROGRESS,
            "scheduled": MatchState.UPCOMING,
            "final": MatchState.COMPLETED,
            "paused": MatchState.SUSPENDED,
            "settled": MatchState.RESOLVED,
        },
    )


def test_match_is_live_only_when_two_sources_agree_and_are_fresh():
    t = tracker()
    assert t.update([sig("live", "a"), sig("inplay", "b")], "g1", 101).tradable_live
    one = tracker().update([sig("live", "a")], "g1", 101)
    assert not one.tradable_live and "insufficient" in one.reason
    stale = tracker().update([sig("live", "a"), sig("live", "b")], "g1", 200)
    assert not stale.tradable_live
    split = tracker().update([sig("live", "a"), sig("paused", "b")], "g1", 101)
    assert not split.tradable_live  # conflicting sources
    unk = tracker().update([sig("weird", "a"), sig("weird", "b")], "g1", 101)
    assert unk.reason == "unmapped_or_ambiguous_status"


def test_illegal_state_regression_is_flagged_and_blocks_trading():
    t = tracker()
    t.update([sig("final", "a"), sig("final", "b")], "g1", 101)
    v = t.update([sig("live", "a", 102), sig("live", "b", 102)], "g1", 103)
    assert v.reason == "illegal_state_transition" and not v.tradable_live and t.anomalies


def test_event_study_detects_planted_drift_and_not_noise():
    rng = random.Random(3)
    n = 600
    entry = [0.5] * n
    drift = [0.5 + 0.03 + rng.gauss(0, 0.05) for _ in range(n)]
    noise = [0.5 + rng.gauss(0, 0.05) for _ in range(n)]
    dirs = [1] * n
    assert event_study(entry, drift, dirs, 0.01).significant
    assert not event_study(entry, noise, dirs, 0.01).significant
    assert "insufficient" in event_study(entry[:50], noise[:50], dirs[:50], 0.0).reason


def test_sport_exposure_limit(tmp_path):
    r = reg(tmp_path, "pure_arb")
    e = DecisionEngine(r, Limits(per_sport=D("6")), available_capital=D("100"))

    def mk(i):
        return Candidate(
            "pure_arb",
            f"e{i}",
            f"g{i}",
            "polymarket",
            D("5"),
            D("1"),
            True,
            True,
            True,
            sport="nba",
        )

    d = e.decide([mk(1), mk(2)])
    assert len(d.selected) == 1 and "sport_exposure_limit" in d.rejected[0][1]


def test_weekly_and_daily_loss_limits_block_trading(tmp_path):
    now = {"t": 1_000_000.0}
    a = CapitalAccount(
        tmp_path / "c.db",
        CapitalPolicy(max_daily_loss=D("5"), max_weekly_loss=D("8"), max_drawdown_pct=D("100")),
        clock=lambda: now["t"],
    )
    a.deposit(D("100"), "d")
    a.recognize_pnl("l1", D("-4"))
    assert a.trading_allowed()
    a.recognize_pnl("l2", D("-1.5"))
    assert a.loss_limit_hit() == "daily_loss_limit" and not a.trading_allowed()
    now["t"] += 2 * 86400  # day passes; week limit not yet hit
    assert a.trading_allowed()
    a.recognize_pnl("l3", D("-3"))
    assert a.loss_limit_hit() == "weekly_loss_limit"


def test_dashboard_is_honest_and_escapes_html(tmp_path):
    (tmp_path / "alerts.jsonl").write_text(
        json.dumps(
            {
                "ts": 1,
                "severity": "error",
                "component": "feed",
                "message": "<script>alert(1)</script>",
            }
        )
        + "\n"
    )
    a = CapitalAccount(tmp_path / "capital.db")
    a.deposit(D("100"), "d")
    a.recognize_pnl("r1", D("2.5"), {"strategy": "pure_arb"})
    page = render_dashboard(tmp_path, now=100.0)
    assert "&lt;script&gt;" in page and "<script>alert" not in page
    assert "2.5" in page and "pure_arb" in page and "UNVALIDATED" in page
    assert page.count("NOT CONNECTED") >= 5  # nothing is faked
