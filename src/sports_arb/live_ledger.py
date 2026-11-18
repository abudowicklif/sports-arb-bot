"""Persistent append-only live execution evidence and conservative risk totals."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sports_arb.errors import SportsArbError
from sports_arb.live_models import OrderState, OrderSubmission, VenueFill
from sports_arb.paths import DATA_DIR


class RiskReservationError(SportsArbError):
    """An atomic shared-capital reservation was rejected."""


SENSITIVE_KEYS = {
    "secret",
    "passphrase",
    "apikey",
    "api_key",
    "private_key",
    "owner",
    "order_owner",
    "trade_owner",
    "user_id",
}


def redact(value: Any) -> Any:
    """Remove API credentials/owner UUIDs before persisting venue payloads."""
    if isinstance(value, dict):
        return {
            key: ("[redacted]" if key.lower() in SENSITIVE_KEYS else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


class LiveLedger:
    def __init__(self, path: Path = DATA_DIR / "live.db") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS rounds (
                id TEXT PRIMARY KEY,
                created_at REAL NOT NULL,
                utc_day TEXT NOT NULL,
                market_id TEXT NOT NULL,
                condition_id TEXT NOT NULL,
                status TEXT NOT NULL,
                requested_shares TEXT NOT NULL,
                committed_cost TEXT NOT NULL,
                cash_flow_before_fees TEXT,
                estimated_fees TEXT,
                estimated_realized_pnl TEXT,
                details_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                round_id TEXT NOT NULL,
                asset_id TEXT NOT NULL,
                side TEXT NOT NULL,
                requested_price TEXT NOT NULL,
                requested_size TEXT NOT NULL,
                status TEXT NOT NULL,
                size_matched TEXT NOT NULL,
                raw_json TEXT NOT NULL,
                updated_at REAL NOT NULL,
                FOREIGN KEY(round_id) REFERENCES rounds(id)
            );
            CREATE TABLE IF NOT EXISTS fills (
                trade_id TEXT NOT NULL,
                order_id TEXT NOT NULL,
                asset_id TEXT NOT NULL,
                side TEXT NOT NULL,
                size TEXT NOT NULL,
                price TEXT NOT NULL,
                status TEXT NOT NULL,
                fee_rate_bps TEXT,
                raw_json TEXT NOT NULL,
                recorded_at REAL NOT NULL,
                PRIMARY KEY(trade_id, order_id)
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                round_id TEXT,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_rounds_day ON rounds(utc_day);
            CREATE INDEX IF NOT EXISTS idx_fills_order ON fills(order_id);
            """
        )
        self._migrate()
        self.db.commit()

    SCHEMA_VERSION = 2

    def _migrate(self) -> None:
        """Additive, idempotent migrations tracked in schema_version."""
        self.db.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        row = self.db.execute("SELECT MAX(version) FROM schema_version").fetchone()
        current = row[0] or 1
        if current < 2:
            rounds = {r[1] for r in self.db.execute("PRAGMA table_info(rounds)")}
            if "venue" not in rounds:
                self.db.execute(
                    "ALTER TABLE rounds ADD COLUMN venue TEXT NOT NULL DEFAULT 'unknown'"
                )
            fills = {r[1] for r in self.db.execute("PRAGMA table_info(fills)")}
            if "fee_amount" not in fills:
                self.db.execute("ALTER TABLE fills ADD COLUMN fee_amount TEXT")
            self.db.execute("INSERT INTO schema_version VALUES (2)")

    def close(self) -> None:
        self.db.close()

    def create_round(
        self,
        market_id: str,
        condition_id: str,
        shares: Decimal,
        committed_cost: Decimal,
        *,
        venue: str = "unknown",
        max_daily_notional: Decimal | None = None,
        max_in_flight_cost: Decimal | None = None,
        max_daily_realized_loss: Decimal | None = None,
    ) -> str:
        """Atomically reserve shared risk before either venue can submit.

        Polymarket and Kalshi workers call this on the same SQLite connection.
        BEGIN IMMEDIATE also protects the caps across separate processes using
        the same ledger, although the normal CLI additionally holds one lock.
        """
        round_id = str(uuid.uuid4())
        now = time.time()
        day = _utc_day(now)
        try:
            self.db.execute("BEGIN IMMEDIATE")
            if max_daily_notional is not None:
                rows = self.db.execute(
                    "SELECT committed_cost FROM rounds WHERE utc_day=?", (day,)
                ).fetchall()
                spent = sum((Decimal(row[0]) for row in rows), Decimal("0"))
                if spent + committed_cost > max_daily_notional:
                    raise RiskReservationError("daily committed-notional limit would be exceeded")
            if max_in_flight_cost is not None:
                rows = self.db.execute(
                    "SELECT committed_cost FROM rounds WHERE status='SUBMITTING'"
                ).fetchall()
                active = sum((Decimal(row[0]) for row in rows), Decimal("0"))
                if active + committed_cost > max_in_flight_cost:
                    raise RiskReservationError("global in-flight exposure limit would be exceeded")
            if max_daily_realized_loss is not None:
                rows = self.db.execute(
                    """SELECT estimated_realized_pnl FROM rounds
                    WHERE utc_day=? AND estimated_realized_pnl IS NOT NULL""",
                    (day,),
                ).fetchall()
                pnl = sum((Decimal(row[0]) for row in rows), Decimal("0"))
                if pnl <= -max_daily_realized_loss:
                    raise RiskReservationError("daily realized-loss limit reached")
            self.db.execute(
                """INSERT INTO rounds
                (id, created_at, utc_day, market_id, condition_id, status,
                 requested_shares, committed_cost, details_json, venue)
                VALUES (?, ?, ?, ?, ?, 'SUBMITTING', ?, ?, '{}', ?)""",
                (
                    round_id,
                    now,
                    day,
                    market_id,
                    condition_id,
                    str(shares),
                    str(committed_cost),
                    venue,
                ),
            )
            self.db.commit()
            return round_id
        except Exception:
            self.db.rollback()
            raise

    def record_submission(
        self,
        round_id: str,
        submission: OrderSubmission,
        *,
        asset_id: str,
        side: str,
        price: Decimal,
        size: Decimal,
    ) -> None:
        if not submission.order_id:
            self.event(round_id, "ORDER_REJECTED", submission.raw)
            return
        self.db.execute(
            """INSERT INTO orders
            (order_id, round_id, asset_id, side, requested_price, requested_size,
             status, size_matched, raw_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, '0', ?, ?)
            ON CONFLICT(order_id) DO UPDATE SET
                status=excluded.status, raw_json=excluded.raw_json,
                updated_at=excluded.updated_at""",
            (
                submission.order_id,
                round_id,
                asset_id,
                side,
                str(price),
                str(size),
                submission.status,
                _json(submission.raw),
                time.time(),
            ),
        )
        self.db.commit()

    def record_state(self, round_id: str, state: OrderState) -> None:
        if not state.order_id:
            return
        self.db.execute(
            """UPDATE orders SET status=?, size_matched=?, raw_json=?, updated_at=?
            WHERE order_id=? AND round_id=?""",
            (
                state.status,
                str(state.size_matched),
                _json(state.raw),
                time.time(),
                state.order_id,
                round_id,
            ),
        )
        self.db.commit()

    def record_fills(self, fills: list[VenueFill]) -> None:
        for fill in fills:
            self.db.execute(
                """INSERT INTO fills
                (trade_id, order_id, asset_id, side, size, price, status,
                 fee_rate_bps, raw_json, recorded_at, fee_amount)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_id, order_id) DO UPDATE SET
                    status=excluded.status, raw_json=excluded.raw_json,
                    fee_amount=excluded.fee_amount""",
                (
                    fill.trade_id,
                    fill.order_id,
                    fill.asset_id,
                    fill.side,
                    str(fill.size),
                    str(fill.price),
                    fill.status,
                    str(fill.fee_rate_bps) if fill.fee_rate_bps is not None else None,
                    _json(fill.raw),
                    time.time(),
                    str(fill.fee_amount) if fill.fee_amount is not None else None,
                ),
            )
        self.db.commit()

    def release_reservation(self, round_id: str) -> None:
        """A round that never filled holds no capital; release its daily reservation."""
        self.db.execute("UPDATE rounds SET committed_cost='0' WHERE id=?", (round_id,))
        self.db.commit()

    def finish_round(
        self,
        round_id: str,
        status: str,
        *,
        cash_flow_before_fees: Decimal,
        estimated_fees: Decimal,
        estimated_realized_pnl: Decimal | None,
        details: dict[str, Any],
    ) -> None:
        self.db.execute(
            """UPDATE rounds SET status=?, cash_flow_before_fees=?,
            estimated_fees=?, estimated_realized_pnl=?, details_json=? WHERE id=?""",
            (
                status,
                str(cash_flow_before_fees),
                str(estimated_fees),
                str(estimated_realized_pnl) if estimated_realized_pnl is not None else None,
                _json(details),
                round_id,
            ),
        )
        self.db.commit()
        if status == "NO_FILL":
            self.release_reservation(round_id)

    def event(self, round_id: str | None, event_type: str, payload: Any) -> None:
        self.db.execute(
            """INSERT INTO events (timestamp, round_id, event_type, payload_json)
            VALUES (?, ?, ?, ?)""",
            (time.time(), round_id, event_type, _json(payload)),
        )
        self.db.commit()

    def daily_committed_cost(self, day: str | None = None) -> Decimal:
        day = day or _utc_day(time.time())
        rows = self.db.execute(
            "SELECT committed_cost FROM rounds WHERE utc_day=?", (day,)
        ).fetchall()
        return sum((Decimal(row["committed_cost"]) for row in rows), Decimal("0"))

    def daily_estimated_realized_pnl(self, day: str | None = None) -> Decimal:
        day = day or _utc_day(time.time())
        rows = self.db.execute(
            """SELECT estimated_realized_pnl FROM rounds
            WHERE utc_day=? AND estimated_realized_pnl IS NOT NULL""",
            (day,),
        ).fetchall()
        return sum((Decimal(row["estimated_realized_pnl"]) for row in rows), Decimal("0"))

    def order_states(self, round_id: str) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM orders WHERE round_id=?", (round_id,)))


def _json(value: Any) -> str:
    return json.dumps(redact(value), separators=(",", ":"), default=str)


def _utc_day(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).date().isoformat()
