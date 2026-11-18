import pytest

from sports_arb.live_config import (
    LIVE_CONFIRM_ENV,
    LIVE_ENV_FLAG,
    LIVE_PHRASE,
    Credentials,
    LiveConfigError,
    LiveRiskConfig,
    assert_live_armed,
)


def valid_env(monkeypatch):
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0x" + "1" * 64)
    monkeypatch.setenv("POLYMARKET_API_KEY", "key")
    monkeypatch.setenv("POLYMARKET_API_SECRET", "secret")
    monkeypatch.setenv("POLYMARKET_API_PASSPHRASE", "pass")
    monkeypatch.setenv("POLYMARKET_SIGNATURE_TYPE", "3")
    monkeypatch.setenv("POLYMARKET_FUNDER_ADDRESS", "0x" + "a" * 40)


def test_credentials_require_funder_for_smart_wallet(monkeypatch):
    valid_env(monkeypatch)
    monkeypatch.delenv("POLYMARKET_FUNDER_ADDRESS")
    with pytest.raises(LiveConfigError, match="FUNDER_ADDRESS"):
        Credentials.from_env()


def test_live_requires_three_independent_confirmations(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(LIVE_ENV_FLAG, raising=False)
    monkeypatch.delenv(LIVE_CONFIRM_ENV, raising=False)
    with pytest.raises(LiveConfigError):
        assert_live_armed("REAL FUNDS")
    monkeypatch.setenv(LIVE_ENV_FLAG, "YES")
    monkeypatch.setenv(LIVE_CONFIRM_ENV, LIVE_PHRASE)
    with pytest.raises(LiveConfigError):
        assert_live_armed("wrong")
    assert_live_armed("REAL FUNDS")


def test_live_risk_has_hard_safety_ceiling():
    from decimal import Decimal

    with pytest.raises(LiveConfigError):
        LiveRiskConfig(max_pair_cost=Decimal("1000")).validate()
