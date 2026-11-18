"""Public, read-only Polymarket CLOB order-book client."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable

from sports_arb.book import OrderBook
from sports_arb.errors import MarketDataError
from sports_arb.http import JsonHttpClient

LOGGER = logging.getLogger(__name__)
CLOB_BASE_URL = "https://clob.polymarket.com"
MAX_BOOKS_PER_REQUEST = 500


class ClobClient:
    def __init__(self, http: JsonHttpClient) -> None:
        self.http = http

    async def get_books(self, asset_ids: Iterable[str]) -> dict[str, OrderBook]:
        """Fetch full snapshots in one batch and index them by asset ID.

        The scanner caps discovery at 250 markets so every request contains at
        most 500 outcome IDs. Keeping both outcomes in the same response reduces
        pair-skew risk, though it does not make the books atomic.
        """
        ids = list(dict.fromkeys(asset_ids))
        if not ids:
            return {}
        if len(ids) > MAX_BOOKS_PER_REQUEST:
            raise ValueError("get_books accepts at most 500 unique asset IDs")

        received_at = time.time()
        response = await self.http.request_json(
            "POST",
            "/books",
            body=[{"token_id": asset_id} for asset_id in ids],
        )
        if not isinstance(response, list):
            raise MarketDataError("CLOB /books response must be an array")

        books: dict[str, OrderBook] = {}
        for raw in response:
            if not isinstance(raw, dict):
                LOGGER.warning("Skipping non-object CLOB book")
                continue
            try:
                book = OrderBook.from_payload(raw, received_at=received_at)
            except MarketDataError as exc:
                LOGGER.warning("Skipping malformed CLOB book: %s", exc)
                continue
            if book.asset_id not in ids:
                LOGGER.warning("CLOB returned an unrequested asset ID; ignoring it")
                continue
            if book.asset_id in books:
                raise MarketDataError(f"duplicate book for asset {book.asset_id}")
            books[book.asset_id] = book
        return books
