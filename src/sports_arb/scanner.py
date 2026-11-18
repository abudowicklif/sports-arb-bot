"""Read-only scan orchestration and JSONL recording."""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from sports_arb.book import OrderBookCache
from sports_arb.clob import ClobClient
from sports_arb.detector import Evaluation, SameMarketDetector
from sports_arb.gamma import GammaClient
from sports_arb.models import SportsMarket

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ScanReport:
    scanned_at: float
    discovered_markets: int
    received_books: int
    evaluations: tuple[Evaluation, ...]

    @property
    def qualifying(self) -> tuple[Evaluation, ...]:
        return tuple(item for item in self.evaluations if item.status == "qualifying")

    @property
    def status_counts(self) -> dict[str, int]:
        return dict(Counter(item.status for item in self.evaluations))


class SportsScanner:
    def __init__(
        self,
        gamma: GammaClient,
        clob: ClobClient,
        detector: SameMarketDetector,
    ) -> None:
        self.gamma = gamma
        self.clob = clob
        self.detector = detector
        self.cache = OrderBookCache()

    async def discover(self, market_types: tuple[str, ...], max_markets: int) -> list[SportsMarket]:
        return await self.gamma.discover_sports_markets(
            market_types=market_types, max_markets=max_markets
        )

    async def scan_once(self, markets: list[SportsMarket]) -> ScanReport:
        asset_ids = [asset_id for market in markets for asset_id in market.asset_ids]
        books = await self.clob.get_books(asset_ids)
        for book in books.values():
            self.cache.put(book)
        evaluations = tuple(self.detector.evaluate(market, self.cache) for market in markets)
        return ScanReport(
            scanned_at=time.time(),
            discovered_markets=len(markets),
            received_books=len(books),
            evaluations=evaluations,
        )


def append_jsonl(path: Path, report: ScanReport, *, include_near_misses: bool = False) -> int:
    """Append immutable scan indications; never overwrite earlier observations."""
    selected = [
        evaluation
        for evaluation in report.evaluations
        if evaluation.opportunity is not None
        and (include_near_misses or evaluation.status == "qualifying")
    ]
    if not selected:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for evaluation in selected:
            record = {
                "type": "theoretical_same_market_indication",
                "mode": "READ_ONLY_NO_ORDERS",
                "scanned_at": report.scanned_at,
                "status": evaluation.status,
                "opportunity": _jsonable(asdict(evaluation.opportunity)),
            }
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")
    return len(selected)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value
