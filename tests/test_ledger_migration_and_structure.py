import random
import sqlite3
from decimal import Decimal as D
from types import SimpleNamespace

from sports_arb.kalshi_gateway import normalize_kalshi_books
from sports_arb.live_ledger import LiveLedger


def test_v1_database_migrates_additively(tmp_path):
    path = tmp_path / "old.db"
    LiveLedger(path).close()
    db = sqlite3.connect(path)
    db.execute("DELETE FROM schema_version")
    db.commit()
    db.close()
    ledger = LiveLedger(path)  # reopen: migration must be idempotent
    cols = {r[1] for r in ledger.db.execute("PRAGMA table_info(rounds)")}
    fills = {r[1] for r in ledger.db.execute("PRAGMA table_info(fills)")}
    assert "venue" in cols and "fee_amount" in fills


def test_uncrossed_kalshi_books_never_show_positive_gross_edge():
    """Structural finding (audit item 4): ask(YES)+ask(NO) >= 1 on any uncrossed book."""
    rng = random.Random(7)
    for _ in range(2000):
        yb = D(rng.randint(1, 98)) / 100
        room = int(((1 - yb) - D("0.01")) * 100)
        if room < 1:
            continue
        nb = D(rng.randint(1, room)) / 100
        resp = SimpleNamespace(
            orderbook_fp=SimpleNamespace(
                yes_dollars=[[str(yb), "10"]], no_dollars=[[str(nb), "10"]]
            )
        )
        b = normalize_kalshi_books("T", resp, received_at=1.0)
        assert 1 - b["T::YES"].best_ask.price - b["T::NO"].best_ask.price < 0
