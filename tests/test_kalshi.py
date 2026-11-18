from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest
from helpers import book, cache_with, market
from kalshi_python_async import BookSide, OrderStatus

from sports_arb.detector import DetectorConfig, SameMarketDetector
from sports_arb.kalshi_config import KalshiCredentials
from sports_arb.kalshi_execution import KalshiTrader
from sports_arb.kalshi_gateway import (
    KalshiGateway,
    normalize_kalshi_books,
    normalize_kalshi_fill,
    normalize_kalshi_order,
    normalize_kalshi_submission,
)
from sports_arb.live_ledger import LiveLedger, RiskReservationError
from sports_arb.live_models import OrderState, OrderSubmission
from sports_arb.models import FeeSchedule


def test_bid_only_kalshi_book_converts_to_two_outcome_books() -> None:
    response = SimpleNamespace(
        orderbook_fp=SimpleNamespace(
            yes_dollars=[["0.7900", "1001.00"]],
            no_dollars=[["0.1400", "151.00"]],
        )
    )
    books = normalize_kalshi_books("T", response, received_at=100.0)
    yes = books["T::YES"]
    no = books["T::NO"]
    assert yes.best_bid.price == Decimal("0.79")
    assert yes.best_ask.price == Decimal("0.86")
    assert yes.best_ask.size == Decimal("151")
    assert no.best_bid.price == Decimal("0.14")
    assert no.best_ask.price == Decimal("0.21")
    assert no.best_ask.size == Decimal("1001")


def test_v2_order_mapping_yes_no_buy_and_sell() -> None:
    gateway = object.__new__(KalshiGateway)
    item = replace(market(), market_id="T", condition_id="T", asset_ids=("T::YES", "T::NO"))
    yes_buy = gateway.order_request(
        item,
        asset_id="T::YES",
        action="BUY",
        price=Decimal("0.40"),
        count=Decimal("2"),
        time_in_force="fill_or_kill",
        client_order_id="a",
    )
    no_buy = gateway.order_request(
        item,
        asset_id="T::NO",
        action="BUY",
        price=Decimal("0.50"),
        count=Decimal("2"),
        time_in_force="fill_or_kill",
        client_order_id="b",
    )
    yes_sell = gateway.order_request(
        item,
        asset_id="T::YES",
        action="SELL",
        price=Decimal("0.40"),
        count=Decimal("2"),
        time_in_force="immediate_or_cancel",
        client_order_id="c",
    )
    no_sell = gateway.order_request(
        item,
        asset_id="T::NO",
        action="SELL",
        price=Decimal("0.50"),
        count=Decimal("2"),
        time_in_force="immediate_or_cancel",
        client_order_id="d",
    )
    assert (yes_buy.side, yes_buy.price) == (BookSide.BID, "0.40")
    assert (no_buy.side, no_buy.price) == (BookSide.ASK, "0.50")
    assert (yes_sell.side, yes_sell.price) == (BookSide.ASK, "0.40")
    assert (no_sell.side, no_sell.price) == (BookSide.BID, "0.50")
    assert not yes_buy.reduce_only
    assert not no_buy.reduce_only
    assert yes_sell.reduce_only
    assert no_sell.reduce_only
    with pytest.raises(Exception, match="whole-contract"):
        gateway.order_request(
            item,
            asset_id="T::YES",
            action="BUY",
            price=Decimal("0.40"),
            count=Decimal("1.5"),
            time_in_force="fill_or_kill",
            client_order_id="bad",
        )


def test_order_and_submission_normalization_is_fail_closed() -> None:
    rejected = normalize_kalshi_submission(
        {"order_id": None, "fill_count": "0", "remaining_count": "0", "error": {"code": "bad"}}
    )
    submission = OrderSubmission.from_raw(rejected)
    assert not submission.accepted
    state = OrderState.from_raw(
        normalize_kalshi_order(
            {
                "order_id": "real-id",
                "outcome_side": "no",
                "status": OrderStatus.EXECUTED,
                "initial_count_fp": "2.00",
                "fill_count_fp": "1.00",
                "yes_price_dollars": "0.60",
                "no_price_dollars": "0.40",
            }
        )
    )
    assert state.order_id == "real-id"
    assert state.status == "EXECUTED"
    assert state.size_matched == Decimal("1")
    assert state.price == Decimal("0.40")


def test_fill_uses_actual_action_and_actual_fee() -> None:
    fill = normalize_kalshi_fill(
        {
            "fill_id": "fill-1",
            "order_id": "order-1",
            "ticker": "T",
            "outcome_side": "no",
            "action": "sell",
            "count_fp": "2.00",
            "yes_price_dollars": "0.60",
            "no_price_dollars": "0.40",
            "fee_cost": "0.012345",
        }
    )
    assert fill.side == "SELL"
    assert fill.asset_id == "T::NO"
    assert fill.price == Decimal("0.40")
    assert fill.estimated_fee(Decimal("0.07")) == Decimal("0.012345")


def test_kalshi_fee_rounds_up_and_detector_uses_whole_contracts() -> None:
    schedule = FeeSchedule(rate=Decimal("0.07"), ceil_quantum=Decimal("0.000001"))
    assert schedule.fee(Decimal("1"), Decimal("0.1234")) == Decimal("0.007573")
    item = replace(
        market(min_size="1", fee_rate="0"),
        market_id="T",
        asset_ids=("T::YES", "T::NO"),
    )
    cache = cache_with(
        book("T::YES", asks=[("0.40", "2.5")], bids=[("0.30", "5")]),
        book("T::NO", asks=[("0.40", "2.5")], bids=[("0.30", "5")]),
    )
    result = SameMarketDetector(
        DetectorConfig(
            min_shares=Decimal("1"),
            max_shares=Decimal("5"),
            max_capital=Decimal("10"),
            min_net_edge_bps=Decimal("1"),
            min_net_profit=Decimal("0"),
            whole_contracts=True,
        )
    ).evaluate(item, cache, now=1000.1)
    assert result.opportunity is not None
    assert result.opportunity.shares == Decimal("2")


def test_shared_risk_reservation_is_atomic(tmp_path) -> None:
    ledger = LiveLedger(tmp_path / "live.db")
    try:
        ledger.create_round(
            "P",
            "P",
            Decimal("5"),
            Decimal("8"),
            max_daily_notional=Decimal("10"),
            max_in_flight_cost=Decimal("10"),
        )
        with pytest.raises(RiskReservationError, match="in-flight"):
            ledger.create_round(
                "K",
                "K",
                Decimal("5"),
                Decimal("3"),
                max_daily_notional=Decimal("20"),
                max_in_flight_cost=Decimal("10"),
            )
        with pytest.raises(RiskReservationError, match="daily committed"):
            ledger.create_round(
                "K",
                "K",
                Decimal("5"),
                Decimal("3"),
                max_daily_notional=Decimal("10"),
                max_in_flight_cost=Decimal("20"),
            )
    finally:
        ledger.close()


def test_flat_kalshi_round_uses_venue_realized_pnl(tmp_path, monkeypatch) -> None:
    asyncio.run(_check_flat_kalshi_round(tmp_path, monkeypatch))


async def _check_flat_kalshi_round(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    ledger = LiveLedger(tmp_path / "live.db")
    round_id = ledger.create_round("T", "T", Decimal("1"), Decimal("1"))
    item = replace(market(), market_id="T", condition_id="T", asset_ids=("T::YES", "T::NO"))
    states = (
        OrderState("o1", "EXECUTED", Decimal("1"), Decimal("1"), Decimal("0.45"), {}),
        OrderState("o2", "EXECUTED", Decimal("1"), Decimal("1"), Decimal("0.55"), {}),
    )
    fills = [
        normalize_kalshi_fill(
            {
                "fill_id": "f1",
                "order_id": "o1",
                "ticker": "T",
                "outcome_side": "yes",
                "action": "buy",
                "count_fp": "1",
                "yes_price_dollars": "0.45",
                "no_price_dollars": "0.55",
                "fee_cost": "0.01",
            }
        ),
        normalize_kalshi_fill(
            {
                "fill_id": "f2",
                "order_id": "o2",
                "ticker": "T",
                "outcome_side": "no",
                "action": "sell",
                "count_fp": "1",
                "yes_price_dollars": "0.45",
                "no_price_dollars": "0.55",
                "fee_cost": "0.01",
            }
        ),
    ]

    class PositionGateway:
        async def positions(self, ticker):
            return [{"ticker": ticker, "position_fp": "0", "realized_pnl_dollars": "0.58"}]

    trader = object.__new__(KalshiTrader)
    trader.gateway = PositionGateway()
    trader.ledger = ledger
    try:
        result = await trader._finish(
            round_id, item, states, [], fills, baseline_realized=Decimal("0.50")
        )
        assert result.status == "FLAT_MATCHED"
        assert result.estimated_realized_pnl == Decimal("0.08")
        assert not (tmp_path / "data/KILL_SWITCH").exists()
    finally:
        ledger.close()


def test_private_key_must_be_protected(tmp_path, monkeypatch) -> None:
    path = tmp_path / "key.pem"
    path.write_text("not-a-key")
    path.chmod(0o644)
    monkeypatch.setenv("KALSHI_API_KEY_ID", "id")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(path))
    with pytest.raises(Exception, match="chmod 600"):
        KalshiCredentials.from_env()
    assert "KALSHI_PRIVATE_KEY" not in os.environ
