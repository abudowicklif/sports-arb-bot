from decimal import Decimal

from helpers import book, market

from sports_arb.live_execution import (
    conservative_cost_at_limits,
    fills_cover_states,
    protected_buy_limit,
)
from sports_arb.live_models import OrderState, VenueFill


def test_missing_trade_rows_never_count_as_complete_pnl_evidence():
    state = OrderState(
        order_id="order-1",
        status="MATCHED",
        original_size=Decimal("5"),
        size_matched=Decimal("5"),
        price=Decimal("0.40"),
        raw={},
    )
    assert not fills_cover_states((state,), [])
    fill = VenueFill(
        trade_id="trade-1",
        order_id="order-1",
        asset_id="asset",
        side="BUY",
        size=Decimal("5"),
        price=Decimal("0.40"),
        status="MATCHED",
        fee_rate_bps=None,
        raw={},
    )
    assert fills_cover_states((state,), [fill])


def test_protected_buy_limit_walks_depth_and_adds_tick():
    snapshot = book("101", [("0.40", "2"), ("0.42", "5")])
    assert protected_buy_limit(snapshot, Decimal("5")) == Decimal("0.43")


def test_cost_at_limits_includes_both_dynamic_fee_curves():
    m = market(fee_rate="0.05")
    cost = conservative_cost_at_limits(m, Decimal("5"), Decimal("0.40"), Decimal("0.50"))
    # Notional 4.50 + fees 0.06 + 0.0625
    assert cost == Decimal("4.6225000")
