from decimal import Decimal

from sports_arb.book import OrderBook


def test_book_normalizes_wire_order_and_duplicate_levels():
    raw = {
        "market": "condition",
        "asset_id": "123",
        "timestamp": "1000000000000",
        "bids": [{"price": "0.40", "size": "5"}],
        # REST commonly returns asks in descending order. Do not trust wire order.
        "asks": [
            {"price": "0.60", "size": "10"},
            {"price": "0.50", "size": "2"},
            {"price": "0.50", "size": "3"},
        ],
        "min_order_size": "1",
        "tick_size": "0.01",
    }
    book = OrderBook.from_payload(raw, received_at=1001)
    assert book.best_ask.price == Decimal("0.50")
    assert book.best_ask.size == Decimal("5")
    assert book.best_bid.price == Decimal("0.40")
    assert book.source_timestamp == 1_000_000_000


def test_cache_rejects_older_snapshot():
    from helpers import book, cache_with

    cache = cache_with(book("101", [("0.40", "10")], timestamp=1000))
    accepted = cache.put(book("101", [("0.20", "10")], timestamp=999))
    assert not accepted
    assert cache.get("101").best_ask.price == Decimal("0.40")
