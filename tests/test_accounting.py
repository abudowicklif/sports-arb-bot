from decimal import Decimal as D

import pytest

from sports_arb.accounting import (
    AUTH_PHRASE,
    CapitalAccount,
    CapitalError,
    CapitalPolicy,
)


def acct(tmp_path, **kw):
    a = CapitalAccount(tmp_path / "c.db", CapitalPolicy(**kw) if kw else CapitalPolicy())
    a.deposit(D("100"), "d1")
    return a


def test_policy_must_sum_to_100():
    with pytest.raises(CapitalError):
        CapitalPolicy(reinvest_pct=D("50"), withdraw_pct=D("25"), reserve_pct=D("15"))


def test_profit_recognition_is_idempotent(tmp_path):
    a = acct(tmp_path)
    assert a.recognize_pnl("round-1", D("10")) is True
    assert a.recognize_pnl("round-1", D("10")) is False  # replay/restart cannot double count
    assert a.net_realized() == D("10")


def test_allocation_60_25_15_with_no_rounding_leak(tmp_path):
    a = acct(tmp_path)
    a.recognize_pnl("r1", D("10.000001"))
    split = a.allocate()
    assert sum(split.values()) == D("10.000001")
    assert split["reinvest"] == D("6.000001") or split["reinvest"] == D("6.000001")
    assert a.operating_capital() == D("100") + a.reinvested()
    assert a.undistributed() == D("0")
    assert a.allocate() == {"reinvest": 0, "withdraw": 0, "reserve": 0}  # nothing left


def test_losses_are_absorbed_before_any_distribution(tmp_path):
    a = acct(tmp_path)
    a.recognize_pnl("r1", D("-4"))
    a.recognize_pnl("r2", D("10"))
    assert a.undistributed() == D("6")  # net, not gross
    a.allocate()
    assert a.reinvested() == D("3.6")


def test_deficit_impairs_operating_capital_and_blocks_distribution(tmp_path):
    a = acct(tmp_path)
    a.recognize_pnl("r1", D("-3"))
    assert a.operating_capital() == D("97")
    assert a.allocate() == {"reinvest": 0, "withdraw": 0, "reserve": 0}


def test_liabilities_are_held_back(tmp_path):
    a = acct(tmp_path)
    a.recognize_pnl("r1", D("10"))
    a.allocate(liabilities=D("4"))
    assert a.undistributed() == D("4")


def test_drawdown_suspends_trading_and_distribution_until_authorized_resume(tmp_path):
    a = acct(tmp_path, max_drawdown_pct=D("10"))
    a.recognize_pnl("win", D("20"))  # peak 120
    a.recognize_pnl("loss", D("-13"))  # 107 -> 10.8% drawdown
    assert a.suspended() and not a.trading_allowed()
    with pytest.raises(CapitalError):
        a.allocate()
    with pytest.raises(CapitalError):
        a.resume("nope")
    a.resume(AUTH_PHRASE)
    assert not a.suspended()


def test_policy_change_requires_authorization_and_is_audited(tmp_path):
    a = acct(tmp_path)
    new = CapitalPolicy(D("50"), D("30"), D("20"))
    with pytest.raises(CapitalError):
        a.set_policy(new, "please")
    a.set_policy(new, AUTH_PHRASE)
    assert a.policy.reinvest_pct == D("50")
    kinds = [r[0] for r in a.db.execute("SELECT kind FROM capital_events")]
    assert "POLICY" in kinds


def test_cannot_withdraw_more_than_allocated(tmp_path):
    a = acct(tmp_path)
    a.recognize_pnl("r1", D("10"))
    a.allocate()
    with pytest.raises(CapitalError):
        a.record_withdrawal(D("3"), "w1")  # only 2.5 withdrawable
    a.record_withdrawal(D("2.5"), "w1")
    assert a.withdrawable() == D("0")


def test_max_trade_cost_never_the_whole_balance(tmp_path):
    a = acct(tmp_path)
    assert a.max_trade_cost() == D("25")
    assert a.max_trade_cost() < a.operating_capital()
