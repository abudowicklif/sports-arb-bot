"""Official Kalshi async SDK gateway for production market and portfolio data."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from decimal import Decimal
from typing import Any

from kalshi_python_async import (
    BookSide,
    Configuration,
    CreateOrderV2Request,
    EventsApi,
    ExchangeApi,
    KalshiClient,
    MarketApi,
    OrdersApi,
    PortfolioApi,
    SelfTradePreventionType,
)
from kalshi_python_async.auth import KalshiAuth

from sports_arb.book import BookLevel, OrderBook
from sports_arb.errors import MarketDataError, SportsArbError
from sports_arb.kalshi_config import KALSHI_API_URL, KalshiCredentials
from sports_arb.live_models import VenueFill
from sports_arb.models import ONE, ZERO, FeeSchedule, SportsMarket, decimal_value

KALSHI_GENERAL_TAKER_RATE = Decimal("0.07")
LOGGER = logging.getLogger(__name__)


class KalshiGatewayError(SportsArbError):
    pass


class KalshiGateway:
    """Real Kalshi production calls. This class has no simulated execution path."""

    def __init__(self, credentials: KalshiCredentials, *, timeout: float = 15.0) -> None:
        configuration = Configuration(host=KALSHI_API_URL)
        self.client = KalshiClient(configuration)
        # SDK 3.35.0's set_kalshi_auth() forgets to import KalshiAuth and raises
        # NameError. Initialize the exact same official auth object directly;
        # request serialization/signing still remains entirely inside the SDK.
        self.client.kalshi_auth = KalshiAuth(credentials.key_id, credentials.private_key_pem)
        self.markets = MarketApi(self.client)
        self.events = EventsApi(self.client)
        self.exchange = ExchangeApi(self.client)
        self.orders = OrdersApi(self.client)
        self.portfolio = PortfolioApi(self.client)
        self.timeout = timeout
        self._series_fees: dict[str, tuple[str, Decimal]] = {}
        self._market_ticks: dict[str, Decimal] = {}

    async def close(self) -> None:
        await self.client.close()

    async def _call(self, awaitable: Any) -> Any:
        try:
            return await asyncio.wait_for(awaitable, timeout=self.timeout)
        except TimeoutError as exc:
            raise KalshiGatewayError("Kalshi request timed out; outcome may be unknown") from exc
        except SportsArbError:
            raise
        except Exception as exc:
            raise KalshiGatewayError(f"Kalshi API request failed: {exc}") from exc

    async def exchange_status(self) -> dict[str, Any]:
        response = await self._call(self.exchange.get_exchange_status())
        return response.to_dict()

    async def balance(self) -> Decimal:
        response = await self._call(self.portfolio.get_balance())
        return decimal_value(response.balance_dollars, "balance_dollars")

    async def open_orders(self, ticker: str | None = None) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            response = await self._call(
                self.orders.get_orders(ticker=ticker, status="resting", limit=200, cursor=cursor)
            )
            result.extend(item.to_dict() for item in response.orders)
            cursor = response.cursor or None
            if not cursor or not response.orders:
                return result

    async def positions(self, ticker: str | None = None) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            response = await self._call(
                self.portfolio.get_positions(ticker=ticker, limit=200, cursor=cursor)
            )
            result.extend(item.to_dict() for item in response.market_positions)
            cursor = response.cursor or None
            if not cursor or not response.market_positions:
                return result

    async def discover_sports_markets(
        self, market_types: tuple[str, ...], max_markets: int
    ) -> list[SportsMarket]:
        if not 1 <= max_markets <= 250:
            raise ValueError("max_markets must be between 1 and 250")
        if not market_types:
            raise ValueError("at least one Kalshi market type is required")
        wanted = {item.lower() for item in market_types}
        result: list[SportsMarket] = []
        seen_tickers: set[str] = set()
        seen_cursors: set[str] = set()
        cursor: str | None = None
        while len(result) < max_markets:
            response = await self._call(
                self.events.get_events(
                    limit=200,
                    cursor=cursor,
                    with_nested_markets=True,
                    status="open",
                )
            )
            for event in response.events:
                if str(event.category or "").casefold() != "sports":
                    continue
                try:
                    fee = await self._event_fee(event)
                except MarketDataError as exc:
                    LOGGER.warning("Skipping Kalshi event %s: %s", event.event_ticker, exc)
                    continue
                for raw_market in event.markets or []:
                    if len(result) >= max_markets:
                        break
                    if raw_market.ticker in seen_tickers:
                        continue
                    try:
                        market = normalize_kalshi_market(raw_market, event, fee)
                    except MarketDataError as exc:
                        LOGGER.warning("Skipping Kalshi market %s: %s", raw_market.ticker, exc)
                        continue
                    seen_tickers.add(raw_market.ticker)
                    if market is not None and market.sports_market_type in wanted:
                        self._market_ticks[market.market_id] = decimal_value(
                            raw_market.price_ranges[0].step, "Kalshi price step"
                        )
                        result.append(market)
            next_cursor = response.cursor or None
            if not next_cursor or not response.events:
                break
            if next_cursor in seen_cursors:
                raise MarketDataError("Kalshi event pagination cursor repeated")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return result

    async def _event_fee(self, event: Any) -> FeeSchedule:
        fee_type = event.fee_type_override
        multiplier = event.fee_multiplier_override
        if fee_type is None or multiplier is None:
            fee_type, multiplier = await self._series_fee(event.series_ticker)
        if str(fee_type) not in {
            "quadratic",
            "quadratic_with_maker_fees",
            "quadratic_with_combo_maker_fees",
        }:
            # The flat schedule is product-specific. Never substitute a generic
            # formula and accidentally call an unprofitable trade profitable.
            raise MarketDataError(f"unsupported Kalshi fee schedule: {fee_type}")
        multiplier_decimal = decimal_value(multiplier, "Kalshi fee multiplier")
        if multiplier_decimal < ZERO:
            raise MarketDataError("Kalshi fee multiplier cannot be negative")
        return FeeSchedule(
            rate=KALSHI_GENERAL_TAKER_RATE * multiplier_decimal,
            ceil_quantum=Decimal("0.000001"),
        )

    async def _series_fee(self, series_ticker: str) -> tuple[str, Decimal]:
        cached = self._series_fees.get(series_ticker)
        if cached is not None:
            return cached
        response = await self._call(self.markets.get_series(series_ticker))
        series = response.series
        result = (
            str(series.fee_type.value),
            decimal_value(series.fee_multiplier, "series fee multiplier"),
        )
        self._series_fees[series_ticker] = result
        return result

    async def get_books(self, asset_ids: list[str]) -> dict[str, OrderBook]:
        tickers = sorted({asset_id.rsplit("::", 1)[0] for asset_id in asset_ids})
        responses = await asyncio.gather(
            *(
                self._call(self.markets.get_market_orderbook(ticker, depth=100))
                for ticker in tickers
            )
        )
        result: dict[str, OrderBook] = {}
        received = time.time()
        for ticker, response in zip(tickers, responses, strict=True):
            result.update(
                normalize_kalshi_books(
                    ticker,
                    response,
                    received_at=received,
                    tick_size=self._market_ticks.get(ticker, Decimal("0.01")),
                )
            )
        return {asset_id: result[asset_id] for asset_id in asset_ids if asset_id in result}

    def order_request(
        self,
        market: SportsMarket,
        *,
        asset_id: str,
        action: str,
        price: Decimal,
        count: Decimal,
        time_in_force: str,
        client_order_id: str,
    ) -> CreateOrderV2Request:
        """Map normalized outcome orders to Kalshi's single YES-priced book."""
        outcome = asset_id.rsplit("::", 1)[-1]
        if outcome not in {"YES", "NO"}:
            raise KalshiGatewayError(f"invalid normalized Kalshi asset: {asset_id}")
        action = action.upper()
        if action not in {"BUY", "SELL"}:
            raise KalshiGatewayError("action must be BUY or SELL")
        if count != count.to_integral_value() or count <= ZERO:
            raise KalshiGatewayError("Kalshi V2 strategy requires a positive whole-contract count")
        if not ZERO < price < ONE:
            raise KalshiGatewayError("Kalshi outcome price must be in (0, 1)")
        # bid == long YES; ask == long NO. Selling one outcome has the same
        # directional exposure as buying the complement. Actual buy/sell cash
        # action is read back from each venue fill, never inferred here.
        book_side = BookSide.BID if (outcome == "YES") == (action == "BUY") else BookSide.ASK
        yes_price = price if outcome == "YES" else ONE - price
        return CreateOrderV2Request(
            ticker=market.market_id,
            client_order_id=client_order_id,
            side=book_side,
            count=format(count, "f"),
            price=format(yes_price, "f"),
            time_in_force=time_in_force,
            post_only=False,
            self_trade_prevention_type=SelfTradePreventionType.TAKER_AT_CROSS,
            cancel_order_on_pause=True,
            reduce_only=action == "SELL",
        )

    async def post_pair_fok(
        self,
        market: SportsMarket,
        *,
        prices: tuple[Decimal, Decimal],
        count: Decimal,
    ) -> list[dict[str, Any]]:
        requests = [
            self.order_request(
                market,
                asset_id=asset_id,
                action="BUY",
                price=price,
                count=count,
                time_in_force="fill_or_kill",
                client_order_id=f"arb-{uuid.uuid4()}",
            )
            for asset_id, price in zip(market.asset_ids, prices, strict=True)
        ]
        # The venue batch is a transport convenience, not an atomic hedge.
        response = await self._call(self.orders.batch_create_orders_v2(orders=requests))
        if len(response.orders) != 2:
            raise KalshiGatewayError("Kalshi batch did not return exactly two leg results")
        return [normalize_kalshi_submission(item.to_dict()) for item in response.orders]

    async def post_ioc(
        self,
        market: SportsMarket,
        *,
        asset_id: str,
        action: str,
        price: Decimal,
        count: Decimal,
    ) -> dict[str, Any]:
        request = self.order_request(
            market,
            asset_id=asset_id,
            action=action,
            price=price,
            count=count,
            time_in_force="immediate_or_cancel",
            client_order_id=f"unwind-{uuid.uuid4()}",
        )
        response = await self._call(self.orders.create_order_v2(**request.to_dict()))
        return normalize_kalshi_submission(response.to_dict())

    async def get_order(self, order_id: str) -> dict[str, Any]:
        response = await self._call(self.orders.get_order(order_id))
        return normalize_kalshi_order(response.order.to_dict())

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        response = await self._call(self.orders.cancel_order_v2(order_id))
        return response.to_dict()

    async def cancel_market(self, ticker: str) -> dict[str, Any]:
        open_orders = await self.open_orders(ticker)
        results = []
        for raw in open_orders:
            order_id = str(raw.get("order_id") or "")
            if order_id:
                results.append(await self.cancel_order(order_id))
        return {"ticker": ticker, "cancel_results": results}

    async def cancel_all(self) -> dict[str, Any]:
        await self._call(self.orders.cancel_all_orders())
        return {"production_cancel_all_requested": True}

    async def recent_fills(self) -> list[dict[str, Any]]:
        response = await self._call(self.portfolio.get_fills(limit=100))
        return [item.to_dict() for item in response.fills]

    async def fills_for_order(self, order_id: str) -> list[VenueFill]:
        result: list[VenueFill] = []
        cursor: str | None = None
        while True:
            response = await self._call(
                self.portfolio.get_fills(order_id=order_id, limit=200, cursor=cursor)
            )
            result.extend(normalize_kalshi_fill(item.to_dict()) for item in response.fills)
            cursor = response.cursor or None
            if not cursor or not response.fills:
                return result


def normalize_kalshi_market(raw: Any, event: Any, fee: FeeSchedule) -> SportsMarket | None:
    if raw.market_type != "binary" or raw.status != "active" or raw.is_provisional:
        return None
    if raw.settlement_bounds_type != "default":
        return None
    steps = {decimal_value(item.step, "Kalshi price step") for item in raw.price_ranges}
    if not steps or len(steps) != 1 or next(iter(steps)) <= ZERO:
        raise MarketDataError(f"Kalshi market {raw.ticker} has missing or non-uniform price steps")
    metadata = event.product_metadata or {}
    kind = str(
        metadata.get("sports_market_type")
        or metadata.get("market_type")
        or classify_kalshi_market(raw.title, raw.subtitle, event.title)
    ).lower()
    return SportsMarket(
        market_id=raw.ticker,
        condition_id=raw.ticker,
        slug=event.event_ticker,
        question=" — ".join(filter(None, [event.title, raw.title, raw.subtitle])),
        version="kalshi-v2",
        outcome_labels=(raw.yes_sub_title or "YES", raw.no_sub_title or "NO"),
        asset_ids=(f"{raw.ticker}::YES", f"{raw.ticker}::NO"),
        sports_market_type=kind,
        fee_schedule=fee,
        order_min_size=ONE,
        liquidity=decimal_value(raw.open_interest_fp, "open_interest_fp"),
        volume_24h=decimal_value(raw.volume_24h_fp, "volume_24h_fp"),
        # expected_expiration_time is an expiration, not a game start: do not mislabel it.
        game_start_time=None,
        seconds_delay=0,
    )


def normalize_kalshi_books(
    ticker: str,
    response: Any,
    *,
    received_at: float | None = None,
    tick_size: Decimal = Decimal("0.01"),
) -> dict[str, OrderBook]:
    raw = response.orderbook_fp
    received = time.time() if received_at is None else received_at
    yes_bids = _pairs(raw.yes_dollars, "yes_dollars")
    no_bids = _pairs(raw.no_dollars, "no_dollars")
    # REST orderbook arrays contain bids only. Complementing the opposite bid
    # produces the executable ask for each outcome.
    yes_asks = sorted(
        (BookLevel(ONE - level.price, level.size) for level in no_bids),
        key=lambda level: level.price,
    )
    no_asks = sorted(
        (BookLevel(ONE - level.price, level.size) for level in yes_bids),
        key=lambda level: level.price,
    )
    common = {
        "condition_id": ticker,
        "source_timestamp": None,
        "received_at": received,
        "min_order_size": ONE,
        "tick_size": tick_size,
        "book_hash": None,
    }
    return {
        f"{ticker}::YES": OrderBook(
            asset_id=f"{ticker}::YES",
            bids=sorted(yes_bids, key=lambda x: x.price, reverse=True),
            asks=yes_asks,
            **common,
        ),
        f"{ticker}::NO": OrderBook(
            asset_id=f"{ticker}::NO",
            bids=sorted(no_bids, key=lambda x: x.price, reverse=True),
            asks=no_asks,
            **common,
        ),
    }


def normalize_kalshi_submission(raw: dict[str, Any]) -> dict[str, Any]:
    error = raw.get("error")
    order_id = raw.get("order_id")
    fill = decimal_value(raw.get("fill_count") or ZERO, "fill_count")
    remaining = decimal_value(raw.get("remaining_count") or ZERO, "remaining_count")
    status = "EXECUTED" if remaining == ZERO and fill > ZERO else "CANCELED"
    if remaining > ZERO:
        status = "RESTING"
    return {
        **raw,
        "orderID": order_id,
        "success": bool(order_id) and error is None,
        "status": status,
        "errorMsg": error,
    }


def normalize_kalshi_order(raw: dict[str, Any]) -> dict[str, Any]:
    outcome = str(raw.get("outcome_side") or "").lower()
    if outcome not in {"yes", "no"}:
        raise MarketDataError("Kalshi order has invalid outcome_side")
    price_field = "yes_price_dollars" if outcome == "yes" else "no_price_dollars"
    return {
        **raw,
        "id": raw.get("order_id"),
        "status": str(getattr(raw.get("status"), "value", raw.get("status")) or "UNKNOWN").upper(),
        "original_size": raw.get("initial_count_fp"),
        "size_matched": raw.get("fill_count_fp"),
        "price": raw.get(price_field),
    }


def normalize_kalshi_fill(raw: dict[str, Any]) -> VenueFill:
    outcome = str(raw.get("outcome_side") or "").lower()
    action = str(raw.get("action") or "").lower()
    if outcome not in {"yes", "no"} or action not in {"buy", "sell"}:
        raise MarketDataError("Kalshi fill lacks canonical outcome_side/action evidence")
    price_field = "yes_price_dollars" if outcome == "yes" else "no_price_dollars"
    return VenueFill(
        trade_id=str(raw["fill_id"]),
        order_id=str(raw["order_id"]),
        asset_id=f"{raw['ticker']}::{outcome.upper()}",
        side=action.upper(),
        size=decimal_value(raw["count_fp"], "fill.count_fp"),
        price=decimal_value(raw[price_field], f"fill.{price_field}"),
        status="CONFIRMED",
        fee_rate_bps=None,
        fee_amount=decimal_value(raw["fee_cost"], "fill.fee_cost"),
        raw=raw,
    )


def classify_kalshi_market(*text: str | None) -> str:
    value = " ".join(item or "" for item in text).casefold()
    if any(word in value for word in ("spread", "handicap", "wins by over")):
        return "spreads"
    if any(word in value for word in ("over/under", "total points", "total goals")):
        return "totals"
    if any(word in value for word in ("championship", "super bowl", "world series", "futures")):
        return "futures"
    if any(
        word in value
        for word in (
            "touchdown",
            "assists",
            "rebounds",
            "strikeouts",
            "home runs",
            "passing",
            "rushing",
            "receiving",
            "career goals",
            "score at least",
        )
    ):
        return "props"
    if any(w in value for w in (" vs ", " vs. ", "winner", "to win", " beat ")):
        return "moneyline"
    return "unknown"


def _pairs(raw: Any, field: str) -> list[BookLevel]:
    result = []
    for item in raw:
        price = decimal_value(item[0], f"{field}.price")
        size = decimal_value(item[1], f"{field}.size")
        if not ZERO < price < ONE or size < ZERO:
            raise MarketDataError(f"invalid Kalshi {field} level")
        if size > ZERO:
            result.append(BookLevel(price=price, size=size))
    return result
