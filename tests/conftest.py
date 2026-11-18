"""Isolate every test run from the real data directory (kill switch, ledger, lock)."""

import os
import tempfile

os.environ["SPORTS_ARB_DATA"] = tempfile.mkdtemp(prefix="sports-arb-test-")

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_kill_switch():
    from sports_arb.live_config import KILL_FILE

    KILL_FILE.unlink(missing_ok=True)
    yield
    KILL_FILE.unlink(missing_ok=True)
