"""Verified match-state tracking. A match is live-action only when its status is verified."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum

from sports_arb.quant.sportsdata import SignalGate, SportsSignal


class MatchState(StrEnum):
    UPCOMING = "upcoming"
    STARTED = "started"
    IN_PROGRESS = "in_progress"
    SUSPENDED = "suspended"
    COMPLETED = "completed_awaiting_settlement"
    RESOLVED = "resolved"


ALLOWED: dict[MatchState, set[MatchState]] = {
    MatchState.UPCOMING: {MatchState.STARTED, MatchState.SUSPENDED, MatchState.RESOLVED},
    MatchState.STARTED: {MatchState.IN_PROGRESS, MatchState.SUSPENDED, MatchState.COMPLETED},
    MatchState.IN_PROGRESS: {MatchState.SUSPENDED, MatchState.COMPLETED},
    MatchState.SUSPENDED: {
        MatchState.IN_PROGRESS,
        MatchState.STARTED,
        MatchState.COMPLETED,
        MatchState.UPCOMING,
    },
    MatchState.COMPLETED: {MatchState.RESOLVED},
    MatchState.RESOLVED: set(),
}


@dataclass(frozen=True, slots=True)
class MatchView:
    state: MatchState | None
    tradable_live: bool
    reason: str


@dataclass(slots=True)
class MatchTracker:
    """``status_map`` translates each provider's raw status strings into MatchState."""

    gate: SignalGate
    status_map: dict[str, MatchState]
    last: dict[str, MatchState] = field(default_factory=dict)
    anomalies: list[str] = field(default_factory=list)

    def update(self, signals: list[SportsSignal], game_id: str, now: float) -> MatchView:
        # Providers spell statuses differently: compare AFTER mapping to a common state.
        normalized = [
            replace(s, value=str(self.status_map.get(s.value, f"unmapped:{s.value}")))
            for s in signals
            if s.kind == "status" and s.game_id == game_id
        ]
        verdict = self.gate.assess(normalized, game_id, now)
        if not verdict.ok:
            return MatchView(self.last.get(game_id), False, f"unverified:{verdict.reason}")
        values = {s.value for s in normalized}
        if len(values) != 1 or next(iter(values)).startswith("unmapped:"):
            return MatchView(self.last.get(game_id), False, "unmapped_or_ambiguous_status")
        new = MatchState(next(iter(values)))
        prev = self.last.get(game_id)
        if prev is not None and new != prev and new not in ALLOWED[prev]:
            self.anomalies.append(f"{game_id}: illegal transition {prev.value}->{new.value}")
            return MatchView(prev, False, "illegal_state_transition")
        self.last[game_id] = new
        return MatchView(new, new == MatchState.IN_PROGRESS, "ok")
