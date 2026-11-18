"""Sports-data signal quality gate. No licensed provider is integrated; this defines the
contract and the safety rules any provider must satisfy before in-play trading is allowed."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SportsSignal:
    game_id: str
    kind: str  # "score", "clock", "status", "red_card", ...
    value: str
    source: str
    event_ts: float  # when it happened per the source
    received_ts: float  # when we received it


@dataclass(frozen=True, slots=True)
class QualityVerdict:
    ok: bool
    reason: str


class SignalGate:
    def __init__(
        self, *, max_age_s: float = 5.0, max_source_lag_s: float = 3.0, min_sources: int = 2
    ) -> None:
        self.max_age_s, self.max_source_lag_s, self.min_sources = (
            max_age_s,
            max_source_lag_s,
            min_sources,
        )

    def assess(self, signals: list[SportsSignal], game_id: str, now: float) -> QualityVerdict:
        """Trade-eligible only with fresh, agreeing data from enough independent sources."""
        relevant = [s for s in signals if s.game_id == game_id]
        if not relevant:
            return QualityVerdict(False, "no_data")
        latest: dict[tuple[str, str], SportsSignal] = {}
        for s in relevant:
            key = (s.kind, s.source)
            if key not in latest or s.received_ts > latest[key].received_ts:
                latest[key] = s
        if any(now - s.received_ts > self.max_age_s for s in latest.values()):
            return QualityVerdict(False, "stale_signal")
        if any(s.received_ts - s.event_ts > self.max_source_lag_s for s in latest.values()):
            return QualityVerdict(False, "source_lag_too_high")
        sources = {s.source for s in latest.values()}
        if len(sources) < self.min_sources:
            return QualityVerdict(False, "insufficient_independent_sources")
        by_kind: dict[str, set[str]] = defaultdict(set)
        for (kind, _src), s in latest.items():
            by_kind[kind].add(s.value)
        if any(len(v) > 1 for v in by_kind.values()):
            return QualityVerdict(False, "conflicting_sources")
        return QualityVerdict(True, "ok")
