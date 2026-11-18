from decimal import Decimal

from sports_arb.live_ledger import LiveLedger, redact
from sports_arb.live_models import OrderState, OrderSubmission, VenueFill


def test_ledger_records_real_evidence_and_decimal_daily_totals(tmp_path):
    ledger = LiveLedger(tmp_path / "live.db")
    round_id = ledger.create_round("m1", "condition", Decimal("5"), Decimal("4.50"))
    submission = OrderSubmission.from_raw(
        {"success": True, "orderID": "order-1", "status": "matched"}
    )
    ledger.record_submission(
        round_id,
        submission,
        asset_id="asset",
        side="BUY",
        price=Decimal("0.40"),
        size=Decimal("5"),
    )
    state = OrderState(
        order_id="order-1",
        status="MATCHED",
        original_size=Decimal("5"),
        size_matched=Decimal("5"),
        price=Decimal("0.40"),
        raw={"id": "order-1", "owner": "secret-owner"},
    )
    ledger.record_state(round_id, state)
    fill = VenueFill(
        trade_id="trade-1",
        order_id="order-1",
        asset_id="asset",
        side="BUY",
        size=Decimal("5"),
        price=Decimal("0.40"),
        status="MATCHED",
        fee_rate_bps=Decimal("500"),
        raw={"id": "trade-1", "owner": "secret-owner"},
    )
    ledger.record_fills([fill])
    ledger.finish_round(
        round_id,
        "FLAT_UNWOUND",
        cash_flow_before_fees=Decimal("-2"),
        estimated_fees=Decimal("0.05"),
        estimated_realized_pnl=Decimal("-2.05"),
        details={},
    )
    assert ledger.daily_committed_cost() == Decimal("4.50")
    assert ledger.daily_estimated_realized_pnl() == Decimal("-2.05")
    stored = ledger.order_states(round_id)[0]
    assert "secret-owner" not in stored["raw_json"]
    ledger.close()


def test_redaction_removes_nested_credentials():
    result = redact({"owner": "uuid", "nested": {"secret": "value", "price": "0.5"}})
    assert result == {
        "owner": "[redacted]",
        "nested": {"secret": "[redacted]", "price": "0.5"},
    }
