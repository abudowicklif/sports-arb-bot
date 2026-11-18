"""Validated order-book snapshots and an in-memory snapshot cache."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sports_arb.errors import MarketDataError
from sports_arb.models import ONE, ZERO, decimal_value


@dataclass(frozen=True, slots=True)
class BookLevel:
    price: Decimal
    size: Decimal


@dataclass(frozen=True, slots=True)
class OrderBook:
    asset_id: str
    condition_id: str
    bids: tuple[BookLevel, ...]  # highest bid first
    asks: tuple[BookLevel, ...]  # lowest ask first
    source_timestamp: float | None
    received_at: float
    min_order_size: Decimal
    tick_size: Decimal | None
    book_hash: str | None

    @classmethod
    def from_payload(cls, raw: dict[str, Any], *, received_at: float | None = None) -> OrderBook:
        asset_id = str(raw.get("asset_id") or raw.get("assetId") or "")
        condition_id = str(raw.get("market") or raw.get("conditionId") or "")
        if not asset_id or not condition_id:
            raise MarketDataError("book is missing asset_id or market/conditionId")

        bids = _levels(raw.get("bids"), "bids")
        asks = _levels(raw.get("asks"), "asks")
        min_order_size = decimal_value(
            raw.get("min_order_size", raw.get("minOrderSize", ZERO)),
            "min_order_size",
        )
        tick_raw = raw.get("tick_size", raw.get("tickSize"))
        tick_size = decimal_value(tick_raw, "tick_size") if tick_raw is not None else None

        return cls(
            asset_id=asset_id,
            condition_id=condition_id,
            bids=tuple(sorted(bids, key=lambda level: level.price, reverse=True)),
            asks=tuple(sorted(asks, key=lambda level: level.price)),
            source_timestamp=_epoch_seconds(raw.get("timestamp")),
            received_at=time.time() if received_at is None else received_at,
            min_order_size=min_order_size,
            tick_size=tick_size,
            book_hash=raw.get("hash"),
        )

    @property
    def best_bid(self) -> BookLevel | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> BookLevel | None:
        return self.asks[0] if self.asks else None

    @property
    def reference_timestamp(self) -> float:
        """Timestamp used for freshness checks, preferring the venue timestamp."""
        return self.source_timestamp if self.source_timestamp is not None else self.received_at

    def age_ms(self, now: float) -> float:
        # A small positive clock offset should not create a negative age.
        return max(0.0, (now - self.reference_timestamp) * 1000)


class OrderBookCache:
    """Last-known-good full snapshots, keyed by outcome asset ID.

    The scanner currently runs cache access on one event loop. A WebSocket
    implementation must additionally handle delta sequencing and re-snapshotting;
    it must not blindly apply deltas to this snapshot-only cache.
    """

    def __init__(self) -> None:
        self._books: dict[str, OrderBook] = {}

    def put(self, book: OrderBook) -> bool:
        previous = self._books.get(book.asset_id)
        if previous is not None:
            if (
                previous.source_timestamp is not None
                and book.source_timestamp is not None
                and book.source_timestamp < previous.source_timestamp
            ):
                return False
            if book.source_timestamp is None and book.received_at < previous.received_at:
                return False
        self._books[book.asset_id] = book
        return True

    def get(self, asset_id: str) -> OrderBook | None:
        return self._books.get(asset_id)

    def __len__(self) -> int:
        return len(self._books)


def _levels(raw: Any, field: str) -> list[BookLevel]:
    if not isinstance(raw, list):
        raise MarketDataError(f"book {field} must be an array")
    aggregated: dict[Decimal, Decimal] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise MarketDataError(f"book {field} contains a non-object level")
        price = decimal_value(item.get("price"), f"{field}.price")
        size = decimal_value(item.get("size"), f"{field}.size")
        if not ZERO <= price <= ONE:
            raise MarketDataError(f"book {field} price outside [0, 1]")
        if size < ZERO:
            raise MarketDataError(f"book {field} size cannot be negative")
        if size == ZERO:
            continue
        aggregated[price] = aggregated.get(price, ZERO) + size
    return [BookLevel(price, size) for price, size in aggregated.items()]


def _epoch_seconds(raw: Any) -> float | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, str) and not raw.replace(".", "", 1).isdigit():
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise MarketDataError(f"invalid book timestamp: {raw!r}") from exc
        return parsed.timestamp()
    value = decimal_value(raw, "timestamp")
    # Raw CLOB timestamps are milliseconds since epoch. Also tolerate seconds.
    if value > Decimal("100000000000"):
        value /= Decimal("1000")
    return float(value)
