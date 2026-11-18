"""Single anchored data directory: kill switch, ledger, lock and recordings."""

from __future__ import annotations

import os
from pathlib import Path

DATA_DIR = Path(
    os.environ.get("SPORTS_ARB_DATA", Path(__file__).resolve().parents[2] / "data")
).resolve()
