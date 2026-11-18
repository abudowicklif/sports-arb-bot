"""Public Data API v2 position reconciliation."""

from __future__ import annotations

from typing import Any

from sports_arb.errors import MarketDataError
from sports_arb.http import JsonHttpClient

DATA_API_BASE_URL = "https://data-api.polymarket.com"


class DataApiClient:
    def __init__(self, http: JsonHttpClient) -> None:
        self.http = http

    async def positions(
        self, wallet_address: str, *, condition_id: str | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"user": wallet_address, "limit": 500}
        if condition_id:
            params["condition"] = condition_id
        response = await self.http.request_json("GET", "/v2/positions", params=params)
        if not isinstance(response, dict) or not isinstance(response.get("data"), list):
            raise MarketDataError("Data API positions response has no data array")
        return response["data"]
