from decimal import Decimal

from sports_arb.live_models import OrderSubmission, fills_for_orders


def test_order_submission_requires_id_and_no_error():
    accepted = OrderSubmission.from_raw(
        {"success": True, "orderID": "order-1", "status": "delayed", "errorMsg": ""}
    )
    assert accepted.accepted
    assert accepted.order_id == "order-1"

    rejected = OrderSubmission.from_raw(
        {"success": True, "orderID": "", "status": "", "errorMsg": "not enough balance"}
    )
    assert not rejected.accepted
    assert rejected.error == "not enough balance"


def test_fill_extraction_supports_taker_and_maker_orders():
    trades = [
        {
            "id": "trade-1",
            "taker_order_id": "taker-order",
            "asset_id": "asset-1",
            "side": "BUY",
            "size": "5",
            "price": "0.40",
            "status": "MATCHED",
            "fee_rate_bps": "500",
        },
        {
            "id": "trade-2",
            "taker_order_id": "someone-else",
            "asset_id": "asset-2",
            "side": "SELL",
            "size": "8",
            "price": "0.60",
            "status": "CONFIRMED",
            "maker_orders": [
                {
                    "order_id": "maker-order",
                    "asset_id": "asset-2",
                    "side": "BUY",
                    "matched_amount": "3",
                    "price": "0.55",
                    "fee_rate_bps": "0",
                }
            ],
        },
    ]
    fills = fills_for_orders(trades, {"taker-order", "maker-order"})
    assert len(fills) == 2
    assert fills[0].size == Decimal("5")
    assert fills[0].estimated_fee(Decimal("0")) == Decimal("0.060000")
    assert fills[1].order_id == "maker-order"
    assert fills[1].size == Decimal("3")
    assert fills[1].price == Decimal("0.55")
