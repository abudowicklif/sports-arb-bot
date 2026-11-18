import pytest

from sports_arb import live_cli
from sports_arb.live_execution import LiveExecutionError


def test_live_process_lock_is_exclusive_and_released(tmp_path, monkeypatch):
    monkeypatch.setattr(live_cli, "LOCK_PATH", tmp_path / "live-process.lock")

    with live_cli._exclusive_live_lock():
        with pytest.raises(LiveExecutionError, match="another live process"):
            with live_cli._exclusive_live_lock():
                pass

    with live_cli._exclusive_live_lock():
        pass
