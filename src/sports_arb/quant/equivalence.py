"""Cross-venue market equivalence. Similar prices or similar titles are NEVER enough.

Pairs exist only if a human-reviewed mapping says the underlying event, outcome definitions
and settlement rules are equivalent. Everything else is non-comparable by default.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

REQUIRED = (
    "event_key",
    "polymarket_condition_id",
    "kalshi_ticker",
    "outcome_map",
    "settlement_equivalent",
    "reviewed_by",
    "reviewed_on",
)


class EquivalenceError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class EquivalentPair:
    event_key: str
    polymarket_condition_id: str
    kalshi_ticker: str
    outcome_map: dict[str, str]  # polymarket outcome label -> kalshi side ("YES"/"NO")
    notes: str = ""


def load_equivalences(path: Path) -> dict[str, EquivalentPair]:
    if not path.exists():
        return {}
    pairs: dict[str, EquivalentPair] = {}
    for row in tomllib.loads(path.read_text()).get("pair", []):
        missing = [k for k in REQUIRED if k not in row]
        if missing:
            raise EquivalenceError(f"mapping missing fields {missing}")
        if row["settlement_equivalent"] is not True:
            continue  # explicitly not equivalent: skip
        if set(row["outcome_map"].values()) != {"YES", "NO"}:
            raise EquivalenceError(f"{row['event_key']}: outcome_map must cover YES and NO")
        if row["event_key"] in pairs:
            raise EquivalenceError(f"duplicate event_key {row['event_key']}")
        pairs[row["event_key"]] = EquivalentPair(
            row["event_key"],
            row["polymarket_condition_id"],
            row["kalshi_ticker"],
            dict(row["outcome_map"]),
            row.get("notes", ""),
        )
    return pairs
