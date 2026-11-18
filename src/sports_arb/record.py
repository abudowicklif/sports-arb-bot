"""Raw Polymarket market-data recorder (append-only JSONL, one file per UTC day).

Stores the universe (with fee schedule, tick, delay, start time as of capture) and raw
``/books`` responses with request-sent and response-received timestamps, so any later
fee, latency or threshold assumption can be replayed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from sports_arb.clob import CLOB_BASE_URL, MAX_BOOKS_PER_REQUEST
from sports_arb.gamma import GAMMA_BASE_URL, GammaClient
from sports_arb.http import JsonHttpClient
from sports_arb.paths import DATA_DIR
from sports_arb.scanner import _jsonable

LOGGER = logging.getLogger(__name__)


def day_path(base: Path, ts: float) -> Path:
    return base / f"polymarket-{datetime.fromtimestamp(ts, UTC):%Y-%m-%d}.jsonl"


def write_row(base: Path, row: dict) -> None:
    base.mkdir(parents=True, exist_ok=True)
    with day_path(base, row["ts"]).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, separators=(",", ":")) + "\n")


async def record_polymarket(
    base: Path,
    *,
    interval: float,
    max_markets: int,
    types: tuple[str, ...],
    iterations: int = 0,
    rediscover_s: float = 300.0,
    on_cycle: Callable[[], None] | None = None,
) -> int:
    rows = 0
    async with JsonHttpClient(GAMMA_BASE_URL) as g, JsonHttpClient(CLOB_BASE_URL) as c:
        gamma, markets, last = GammaClient(g), [], 0.0
        n = 0
        while iterations == 0 or n < iterations:
            t0 = time.time()
            if not markets or t0 - last > rediscover_s:
                markets = await gamma.discover_sports_markets(
                    market_types=types, max_markets=max_markets
                )
                last = t0
                write_row(
                    base,
                    {"k": "universe", "ts": t0, "markets": [_jsonable(asdict(m)) for m in markets]},
                )
                rows += 1
            ids = list(dict.fromkeys(a for m in markets for a in m.asset_ids))
            for start in range(0, len(ids), MAX_BOOKS_PER_REQUEST):
                chunk = ids[start : start + MAX_BOOKS_PER_REQUEST]
                sent = time.time()
                raw = await c.request_json("POST", "/books", body=[{"token_id": i} for i in chunk])
                recv = time.time()
                write_row(
                    base,
                    {
                        "k": "books",
                        "ts": recv,
                        "venue": "polymarket",
                        "sent": sent,
                        "recv": recv,
                        "raw": raw,
                    },
                )
                rows += 1
            n += 1
            if on_cycle is not None:
                on_cycle()
            await asyncio.sleep(max(0.0, interval - (time.time() - t0)))
    return rows


def main() -> None:
    p = argparse.ArgumentParser(prog="sports-arb-record", description=__doc__)
    p.add_argument("--out", type=Path, default=DATA_DIR / "recordings")
    p.add_argument("--interval", type=float, default=2.0)
    p.add_argument("--max-markets", type=int, default=250)
    p.add_argument("--market-types", nargs="+", default=["moneyline", "spreads", "totals"])
    p.add_argument("--iterations", type=int, default=0, help="0 = run until interrupted")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    try:
        asyncio.run(
            record_polymarket(
                a.out,
                interval=a.interval,
                max_markets=a.max_markets,
                types=tuple(a.market_types),
                iterations=a.iterations,
            )
        )
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
