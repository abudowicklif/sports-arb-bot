"""Scripted-venue scenarios for LiveTrader.execute: the paths that cost real money."""

import asyncio
import time
from decimal import Decimal as D

import pytest
from helpers import book, market

from sports_arb.live_config import KILL_FILE, LiveRiskConfig
from sports_arb.live_execution import LiveExecutionError, LiveTrader
from sports_arb.live_ledger import LiveLedger


class FakeClob:
    def __init__(self, bids=("0.30", "0.30")):
        self.bids = bids

    async def get_books(self, ids):
        now = time.time()
        out = {}
        for i, a in enumerate(ids):
            out[a] = book(a, [("0.44", "100")], timestamp=now, bids=[(self.bids[i % 2], "100")])
        return out


class FakeStream:
    def __init__(self, healthy=True):
        self.healthy = healthy

    async def start(self):
        pass

    async def stop(self):
        pass

    def drain(self):
        return []


class FakeData:
    async def positions(self, address, condition_id=None):
        return []


class FakeGateway:
    account_address = "0xabc"

    def __init__(self, script):
        self.script = script  # {"leg0": "fill"|"none", "leg1": ..., "unwind": ...}
        self.signed = []
        self.orders = {}
        self.trades = []
        self.cancelled_markets = []
        self.pair_calls = 0

    def open_orders(self):
        return []

    def collateral_balance(self):
        return D("100")

    def heartbeat(self, hid):
        return {"heartbeat_id": "hb"}

    def sign_order(self, market, **kw):
        self.signed.append(kw)
        return kw

    def post_pair_fok(self, pair):
        self.pair_calls += 1
        if self.script.get("submit_raises"):
            raise ConnectionError("network died after send")
        out = []
        for i, kw in enumerate(pair):
            oid = f"o{i}"
            fill = self.script[f"leg{i}"] == "fill"
            size = kw["size"] if fill else 0
            self.orders[oid] = {
                "id": oid,
                "status": "MATCHED" if fill else "UNMATCHED",
                "original_size": str(kw["size"]),
                "size_matched": str(size),
                "price": str(kw["price"]),
            }
            if fill:
                self._trade(f"t{i}", oid, kw, "BUY")
            out.append(
                {"success": True, "orderID": oid, "status": "matched" if fill else "unmatched"}
            )
        return out

    def post_fak(self, kw):
        oid = f"u{len(self.orders)}"
        ok = self.script.get("unwind") == "fill"
        size = kw["size"] if ok else 0
        self.orders[oid] = {
            "id": oid,
            "status": "MATCHED" if ok else "UNMATCHED",
            "original_size": str(kw["size"]),
            "size_matched": str(size),
            "price": str(kw["price"]),
        }
        if ok:
            self._trade(f"tu{oid}", oid, kw, "SELL")
        return {"success": True, "orderID": oid, "status": "matched" if ok else "unmatched"}

    def _trade(self, tid, oid, kw, side):
        self.trades.append(
            {
                "id": tid,
                "taker_order_id": oid,
                "asset_id": kw["asset_id"],
                "side": side,
                "size": str(kw["size"]),
                "price": str(kw["price"]),
                "status": "MATCHED",
            }
        )

    def get_order(self, oid):
        return self.orders[oid]

    def market_trades(self, cid, first_page_only=False):
        return self.trades

    def cancel_order(self, oid):
        return {}

    def cancel_market(self, cid):
        self.cancelled_markets.append(cid)
        return {"canceled": []}


def trader(tmp_path, script, *, stream_ok=True, risk=None, ledger=None):
    gw = FakeGateway(script)
    t = LiveTrader(
        gamma=None,
        clob=FakeClob(),
        data_api=FakeData(),
        gateway=gw,
        user_stream=FakeStream(stream_ok),
        ledger=ledger or LiveLedger(tmp_path / "l.db"),
        risk=risk or LiveRiskConfig(),
    )
    return t, gw


def run_exec(t):
    async def go():
        books = await t.clob.get_books(("101", "202"))
        from sports_arb.book import OrderBookCache

        cache = OrderBookCache()
        for b in books.values():
            cache.put(b)
        ev = t.detector.evaluate(market(), cache)
        assert ev.status == "qualifying", ev.status
        return await t.execute(market(), ev.opportunity)

    return asyncio.run(go())


def test_both_legs_fill_is_hedged_open(tmp_path):
    t, gw = trader(tmp_path, {"leg0": "fill", "leg1": "fill"})
    r = run_exec(t)
    assert r.status == "HEDGED_OPEN" and gw.pair_calls == 1
    assert not KILL_FILE.exists()


def test_neither_leg_fills_is_no_fill_and_releases_daily_reservation(tmp_path):
    ledger = LiveLedger(tmp_path / "l.db")
    t, _ = trader(tmp_path, {"leg0": "none", "leg1": "none"}, ledger=ledger)
    assert run_exec(t).status == "NO_FILL"
    assert ledger.daily_committed_cost() == D("0")


def test_one_leg_fills_then_unwinds_flat_with_loss(tmp_path):
    t, gw = trader(tmp_path, {"leg0": "fill", "leg1": "none", "unwind": "fill"})
    r = run_exec(t)
    assert r.status == "FLAT_UNWOUND"
    assert r.estimated_realized_pnl is not None and r.estimated_realized_pnl < 0
    assert not KILL_FILE.exists()
    assert any(s["side"] == "SELL" for s in gw.signed)


def test_failed_unwind_halts_with_kill_switch(tmp_path, monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)
    t, _ = trader(tmp_path, {"leg0": "fill", "leg1": "none", "unwind": "none"})
    r = run_exec(t)
    assert r.status == "EXPOSED_HALTED"
    assert KILL_FILE.exists()


async def _instant_sleep(_):
    return None


def test_unknown_submission_never_retries_and_requests_market_cancel(tmp_path):
    t, gw = trader(tmp_path, {"submit_raises": True})
    with pytest.raises(LiveExecutionError, match="outcome is unknown"):
        run_exec(t)
    assert gw.pair_calls == 1 and set(gw.cancelled_markets) == {"condition-1"}


def test_unhealthy_stream_blocks_before_any_order(tmp_path):
    t, gw = trader(tmp_path, {"leg0": "fill", "leg1": "fill"}, stream_ok=False)
    with pytest.raises(LiveExecutionError, match="not healthy"):
        run_exec(t)
    assert gw.pair_calls == 0 and gw.signed == []


def test_daily_notional_limit_blocks_second_round(tmp_path):
    risk = LiveRiskConfig(max_daily_notional=D("5"), max_in_flight_cost=D("5"))
    t, gw = trader(tmp_path, {"leg0": "fill", "leg1": "fill"}, risk=risk)
    assert run_exec(t).status == "HEDGED_OPEN"
    with pytest.raises(LiveExecutionError, match="daily committed-notional"):
        run_exec(t)
    assert gw.pair_calls == 1


def test_daily_loss_limit_blocks_trading(tmp_path):
    ledger = LiveLedger(tmp_path / "l.db")
    risk = LiveRiskConfig(max_daily_realized_loss=D("0.5"))
    t, gw = trader(
        tmp_path, {"leg0": "fill", "leg1": "none", "unwind": "fill"}, risk=risk, ledger=ledger
    )
    assert run_exec(t).status == "FLAT_UNWOUND"
    assert ledger.daily_estimated_realized_pnl() <= D("-0.5")
    with pytest.raises(LiveExecutionError, match="loss"):
        run_exec(t)
    assert gw.pair_calls == 1


def test_restart_recovery_ledger_survives_reopen(tmp_path):
    ledger = LiveLedger(tmp_path / "l.db")
    t, _ = trader(tmp_path, {"leg0": "fill", "leg1": "fill"}, ledger=ledger)
    run_exec(t)
    committed = ledger.daily_committed_cost()
    ledger.close()
    assert LiveLedger(tmp_path / "l.db").daily_committed_cost() == committed
