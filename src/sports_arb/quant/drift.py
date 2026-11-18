"""Model drift detection and automatic suspension."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


@dataclass(slots=True)
class DriftMonitor:
    """Tracks rolling (baseline_loss - model_loss) on RESOLVED markets.

    If the model's rolling advantage over the market falls to <= ``min_gain`` once
    ``min_samples`` resolutions exist, the model is suspended until a human re-approves it.
    """

    window: int = 100
    min_samples: int = 40
    min_gain: float = 0.0
    gains: deque = field(default_factory=deque)
    suspended: bool = False
    reason: str = ""

    def record(self, baseline_loss: float, model_loss: float) -> None:
        self.gains.append(baseline_loss - model_loss)
        while len(self.gains) > self.window:
            self.gains.popleft()
        if len(self.gains) >= self.min_samples:
            avg = sum(self.gains) / len(self.gains)
            if avg <= self.min_gain and not self.suspended:
                self.suspended = True
                self.reason = f"rolling gain {avg:.5f} <= {self.min_gain} over {len(self.gains)}"

    def rolling_gain(self) -> float:
        return sum(self.gains) / len(self.gains) if self.gains else 0.0

    def reapprove(self, phrase: str) -> None:
        if phrase != "AUTHORIZE MODEL RESUME":
            raise PermissionError("model resume requires the authorization phrase")
        self.suspended, self.reason = False, ""
        self.gains.clear()
