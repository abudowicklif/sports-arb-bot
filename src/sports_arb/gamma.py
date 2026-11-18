"""Sports market discovery through the public Gamma API."""

from __future__ import annotations

import logging
from typing import Any

from sports_arb.errors import MarketDataError
from sports_arb.http import JsonHttpClient
from sports_arb.models import SportsMarket

LOGGER = logging.getLogger(__name__)
GAMMA_BASE_URL = "https://gamma-api.polymarket.com"


class GammaClient:
    def __init__(self, http: JsonHttpClient) -> None:
        self.http = http

    async def discover_sports_markets(
        self,
        *,
        market_types: tuple[str, ...] = ("moneyline", "spreads", "totals"),
        max_markets: int = 100,
        max_pages: int = 25,
    ) -> list[SportsMarket]:
        """Return active, orderable two-outcome sports markets.

        The keyset endpoint currently permits at most 100 rows per request.
        Volume is only a discovery/ranking hint; execution sizing uses book depth.
        """
        if not 1 <= max_markets <= 250:
            raise ValueError("max_markets must be between 1 and 250")
        if not market_types:
            raise ValueError("at least one sports market type is required")

        markets: list[SportsMarket] = []
        seen_conditions: set[str] = set()
        # Gamma's singular sports_market_types parameter reliably filters one
        # value per request. Query each requested type, then merge and rank the
        # results instead of assuming a comma-separated list means logical OR.
        for market_type in dict.fromkeys(market_types):
            markets.extend(
                await self._discover_one_type(
                    market_type,
                    max_markets=max_markets,
                    max_pages=max_pages,
                    seen_conditions=seen_conditions,
                )
            )
        markets.sort(key=lambda item: item.volume_24h, reverse=True)
        return markets[:max_markets]

    async def _discover_one_type(
        self,
        market_type: str,
        *,
        max_markets: int,
        max_pages: int,
        seen_conditions: set[str],
    ) -> list[SportsMarket]:
        markets: list[SportsMarket] = []
        seen_cursors: set[str] = set()
        cursor: str | None = None

        for _ in range(max_pages):
            params: dict[str, Any] = {
                "sports_market_types": market_type,
                "closed": "false",
                "active": "true",
                "limit": min(100, max_markets - len(markets)),
                "order": "volume24hr",
                "ascending": "false",
            }
            if cursor:
                params["after_cursor"] = cursor
            page = await self.http.request_json("GET", "/markets/keyset", params=params)
            if not isinstance(page, dict) or not isinstance(page.get("markets"), list):
                raise MarketDataError("Gamma keyset response has no markets array")

            for raw in page["markets"]:
                if not isinstance(raw, dict):
                    LOGGER.warning("Skipping non-object Gamma market")
                    continue
                if not self._is_orderable(raw):
                    continue
                condition_id = str(raw.get("conditionId") or "")
                if not condition_id or condition_id in seen_conditions:
                    continue
                try:
                    market = SportsMarket.from_gamma(raw)
                except MarketDataError as exc:
                    # One malformed market must not blind the entire scanner. The
                    # skip is explicit in logs; it is never treated as fee-free.
                    LOGGER.warning("Skipping market id=%s: %s", raw.get("id"), exc)
                    continue
                markets.append(market)
                seen_conditions.add(condition_id)
                if len(markets) >= max_markets:
                    return markets

            next_cursor = page.get("next_cursor")
            if not next_cursor or next_cursor == "LTE=":
                break
            next_cursor = str(next_cursor)
            if next_cursor in seen_cursors:
                raise MarketDataError("Gamma pagination cursor repeated")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        return markets

    @staticmethod
    def _is_orderable(raw: dict[str, Any]) -> bool:
        return (
            raw.get("active") is True
            and raw.get("closed") is False
            and raw.get("acceptingOrders") is True
            and raw.get("enableOrderBook") is True
            and bool(raw.get("sportsMarketType"))
        )
