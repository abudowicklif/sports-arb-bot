from __future__ import annotations

import asyncio
from argparse import Namespace

import pytest

from sports_arb.errors import SportsArbError
from sports_arb.live_config import KILL_FILE
from sports_arb.multi_cli import _run_concurrent, run


def test_workers_are_actually_concurrent(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    asyncio.run(_check_concurrent_workers())


async def _check_concurrent_workers() -> None:
    started: list[str] = []
    both_started = asyncio.Event()

    class Gateway:
        def cancel_all(self):
            return {}

        async def async_cancel_all(self):
            return {}

    class Trader:
        def __init__(self, name: str, kalshi: bool = False) -> None:
            self.name = name
            self.gateway = Gateway()
            self.kalshi = kalshi

        async def trade_best(self, market_types, max_markets):
            started.append(self.name)
            if len(started) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=1)
            return None

    args = Namespace(
        continuous=False,
        poll_interval=0,
        market_types=["moneyline"],
        max_markets=1,
    )
    await _run_concurrent(Trader("polymarket"), Trader("kalshi", True), args)
    assert set(started) == {"polymarket", "kalshi"}
    assert not KILL_FILE.exists()


def test_kill_file_precedes_credential_validation(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    for name in (
        "POLYMARKET_PRIVATE_KEY",
        "POLYMARKET_API_KEY",
        "POLYMARKET_API_SECRET",
        "POLYMARKET_API_PASSPHRASE",
        "KALSHI_API_KEY_ID",
        "KALSHI_PRIVATE_KEY_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SportsArbError, match="missing environment"):
        asyncio.run(run(Namespace(command="kill")))
    assert KILL_FILE.exists()
    assert "manual multi-venue CLI kill" in KILL_FILE.read_text()
