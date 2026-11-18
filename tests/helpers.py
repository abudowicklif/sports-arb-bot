from __future__ import annotations

from decimal import Decimal

from sports_arb.book import BookLevel, OrderBook, OrderBookCache
from sports_arb.models import FeeSchedule, SportsMarket


def market(*, fee_rate: str = "0", min_size: str = "1") -> SportsMarket:
    return SportsMarket(
        market_id="m1",
        condition_id="condition-1",
        slug="test-market",
        question="Team A vs Team B",
        version="v1",
        outcome_labels=("Team A", "Team B"),
        asset_ids=("101", "202"),
        sports_market_type="moneyline",
        fee_schedule=FeeSchedule(rate=Decimal(fee_rate)),
        order_min_size=Decimal(min_size),
        liquidity=Decimal("1000"),
        volume_24h=Decimal("1000"),
        game_start_time=None,
        seconds_delay=1,
    )


def book(
    asset_id: str,
    asks: list[tuple[str, str]],
    *,
    timestamp: float = 1_000.0,
    bids: list[tuple[str, str]] | None = None,
) -> OrderBook:
    return OrderBook(
        asset_id=asset_id,
        condition_id="condition-1",
        bids=tuple(BookLevel(Decimal(price), Decimal(size)) for price, size in (bids or [])),
        asks=tuple(
            sorted(
                (BookLevel(Decimal(price), Decimal(size)) for price, size in asks),
                key=lambda level: level.price,
            )
        ),
        source_timestamp=timestamp,
        received_at=timestamp,
        min_order_size=Decimal("1"),
        tick_size=Decimal("0.01"),
        book_hash=None,
    )


def cache_with(*books: OrderBook) -> OrderBookCache:
    cache = OrderBookCache()
    for item in books:
        cache.put(item)
    return cache
