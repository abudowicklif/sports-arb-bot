"""Strategy/model validation registry: nothing influences live decisions until a human
approves it with evidence on file. Default state for everything is UNVALIDATED."""

from __future__ import annotations

import json
import time
from pathlib import Path

from sports_arb.paths import DATA_DIR

AUTH = "AUTHORIZE LIVE STRATEGY"
STRATEGIES = ("pure_arb", "stat_arb", "hybrid")


class ValidationRegistry:
    def __init__(self, path: Path = DATA_DIR / "validation_registry.json") -> None:
        self.path = path
        self.state: dict[str, dict] = json.loads(path.read_text()) if path.exists() else {}

    def status(self, name: str) -> str:
        return self.state.get(name, {}).get("status", "UNVALIDATED")

    def approved(self, name: str) -> bool:
        return self.status(name) == "APPROVED_FOR_LIVE"

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2, default=str))
        tmp.replace(self.path)

    def record_evidence(self, name: str, evidence: dict) -> None:
        """Evidence (walk-forward/gate report) never approves by itself."""
        entry = self.state.setdefault(name, {"status": "UNVALIDATED"})
        entry["evidence"] = evidence
        entry["evidence_ts"] = time.time()
        if entry["status"] == "APPROVED_FOR_LIVE":
            entry["status"] = "UNVALIDATED"  # new evidence requires re-approval
        self._save()

    def approve(self, name: str, phrase: str) -> None:
        if phrase != AUTH:
            raise PermissionError("live approval requires the authorization phrase")
        entry = self.state.get(name)
        if not entry or "evidence" not in entry:
            raise PermissionError("cannot approve without recorded validation evidence")
        entry["status"] = "APPROVED_FOR_LIVE"
        entry["approved_ts"] = time.time()
        self._save()

    def suspend(self, name: str, reason: str) -> None:
        entry = self.state.setdefault(name, {})
        entry["status"] = "SUSPENDED"
        entry["reason"] = reason
        self._save()
