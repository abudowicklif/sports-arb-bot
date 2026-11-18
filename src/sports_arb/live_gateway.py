"""Real Polymarket production gateway backed by py-clob-client-v2.

Every order method in this class signs and/or submits genuine orders to
https://clob.polymarket.com. There is intentionally no mock transport or
simulation branch. SDK calls are synchronous and should be invoked with
asyncio.to_thread by the async execution engine.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from py_clob_client_v2 import (
    ApiCreds,
    AssetType,
    BalanceAllowanceParams,
    ClobClient,
    OpenOrderParams,
    OrderArgs,
    OrderMarketCancelParams,
    OrderPayload,
    OrderType,
    PartialCreateOrderOptions,
    PostOrdersV2Args,
    TradeParams,
)

from sports_arb.live_config import Credentials
from sports_arb.models import SportsMarket

PRODUCTION_CLOB_HOST = "https://clob.polymarket.com"
POLYGON_CHAIN_ID = 137


class PolymarketLiveGateway:
    def __init__(self, credentials: Credentials) -> None:
        self.credentials = credentials
        self.client = ClobClient(
            host=PRODUCTION_CLOB_HOST,
            chain_id=POLYGON_CHAIN_ID,
            key=credentials.private_key,
            creds=ApiCreds(
                api_key=credentials.api_key,
                api_secret=credentials.api_secret,
                api_passphrase=credentials.api_passphrase,
            ),
            signature_type=credentials.signature_type,
            funder=credentials.funder_address,
            use_server_time=True,
            retry_on_error=False,
        )
        self.account_address = credentials.funder_address or self.client.get_address()

    def get_ok(self) -> dict[str, Any]:
        return self.client.get_ok()

    def collateral(self, *, refresh: bool = False) -> dict[str, Any]:
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        if refresh:
            self.client.update_balance_allowance(params)
        return self.client.get_balance_allowance(params)

    def collateral_balance(self) -> Decimal:
        raw = self.collateral(refresh=False).get("balance", "0")
        return Decimal(str(raw)) / Decimal("1000000")

    def open_orders(self, condition_id: str | None = None) -> list[dict[str, Any]]:
        params = OpenOrderParams(market=condition_id) if condition_id else None
        return self.client.get_open_orders(params)

    def market_trades(
        self, condition_id: str, *, first_page_only: bool = False
    ) -> list[dict[str, Any]]:
        return self.client.get_trades(
            TradeParams(market=condition_id), only_first_page=first_page_only
        )

    def sign_order(
        self,
        market: SportsMarket,
        *,
        asset_id: str,
        side: str,
        price: Decimal,
        size: Decimal,
        tick_size: Decimal,
    ):
        """Create a genuine EIP-712 signed order without submitting it."""
        kwargs: dict[str, Any] = {
            "price": float(price),
            "size": float(size),
            "side": side,
            # Do not pass user_usdc_balance: SDK auto-resizing would break the
            # equal-share requirement for a complete-set pair.
        }
        if market.version == "v1":
            if asset_id not in market.asset_ids:
                raise ValueError("asset ID does not belong to market")
            kwargs["token_id"] = asset_id
            options = PartialCreateOrderOptions(tick_size=str(tick_size), version=2)
        elif market.version == "v2":
            if asset_id not in market.asset_ids:
                raise ValueError("asset ID does not belong to market")
            kwargs["position_id"] = asset_id
            options = PartialCreateOrderOptions(tick_size=str(tick_size))
        else:
            raise ValueError(f"unsupported market version: {market.version}")
        return self.client.create_order(OrderArgs(**kwargs), options=options)

    def post_pair_fok(self, signed_orders: tuple[Any, Any]) -> list[dict[str, Any]]:
        """Submit two real FOK orders in one non-atomic batch request."""
        response = self.client.post_orders(
            [
                PostOrdersV2Args(order=signed_orders[0], orderType=OrderType.FOK),
                PostOrdersV2Args(order=signed_orders[1], orderType=OrderType.FOK),
            ]
        )
        if not isinstance(response, list) or len(response) != 2:
            raise RuntimeError(f"unexpected two-order response: {response!r}")
        return response

    def post_fak(self, signed_order: Any) -> dict[str, Any]:
        response = self.client.post_order(signed_order, order_type=OrderType.FAK)
        if not isinstance(response, dict):
            raise RuntimeError(f"unexpected unwind order response: {response!r}")
        return response

    def get_order(self, order_id: str) -> dict[str, Any]:
        return self.client.get_order(order_id)

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        return self.client.cancel_order(OrderPayload(orderID=order_id))

    def cancel_orders(self, order_ids: list[str]) -> dict[str, Any]:
        return self.client.cancel_orders(order_ids)

    def cancel_market(self, condition_id: str) -> dict[str, Any]:
        return self.client.cancel_market_orders(OrderMarketCancelParams(market=condition_id))

    def cancel_all(self) -> dict[str, Any]:
        return self.client.cancel_all()

    def heartbeat(self, heartbeat_id: str = "") -> dict[str, Any]:
        return self.client.post_heartbeat(heartbeat_id)
