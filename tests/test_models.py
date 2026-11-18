from decimal import Decimal

import pytest

from sports_arb.errors import MarketDataError
from sports_arb.models import FeeSchedule, SportsMarket


def gamma_market(version="v1"):
    return {
        "id": "1",
        "conditionId": "0xcondition",
        "slug": "a-v-b",
        "question": "A vs B",
        "version": version,
        "sportsMarketType": "moneyline",
        "outcomes": '["A", "B"]',
        "clobTokenIds": '["111", "222"]',
        "positionIds": ["999", "888"],
        "feesEnabled": True,
        "feeSchedule": {
            "rate": 0.05,
            "exponent": 1,
            "takerOnly": True,
            "rebateRate": 0.15,
        },
    }


def test_v1_uses_clob_tokens_even_when_position_ids_exist():
    parsed = SportsMarket.from_gamma(gamma_market("v1"))
    assert parsed.asset_ids == ("111", "222")
    assert parsed.outcome_labels == ("A", "B")
    assert parsed.fee_schedule.rate == Decimal("0.05")


def test_v2_uses_position_ids():
    parsed = SportsMarket.from_gamma(gamma_market("v2"))
    assert parsed.asset_ids == ("999", "888")


def test_unknown_fee_status_is_not_assumed_free():
    raw = gamma_market()
    raw["feesEnabled"] = None
    with pytest.raises(MarketDataError, match="feesEnabled"):
        SportsMarket.from_gamma(raw)


def test_enabled_fees_require_schedule():
    raw = gamma_market()
    raw["feeSchedule"] = None
    with pytest.raises(MarketDataError, match="feeSchedule"):
        SportsMarket.from_gamma(raw)


def test_sports_fee_matches_documented_100_share_example():
    schedule = FeeSchedule(rate=Decimal("0.05"))
    # 100 * 0.05 * 0.5 * (1 - 0.5) = 1.25
    assert schedule.fee(Decimal("100"), Decimal("0.5")) == Decimal("1.25")


def test_fee_free_market_has_zero_fee():
    raw = gamma_market()
    raw["feesEnabled"] = False
    raw["feeSchedule"] = None
    parsed = SportsMarket.from_gamma(raw)
    assert parsed.fee_schedule.fee(Decimal("100"), Decimal("0.5")) == 0
