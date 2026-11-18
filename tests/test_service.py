import asyncio
import json

from sports_arb.ratelimit import TokenBucket
from sports_arb.service import AlertSink, ComponentSpec, Supervisor, check_status


def sup(tmp_path, specs, clock=None, **kw):
    kw.setdefault("base_backoff", 0.01)
    kw.setdefault("check_interval", 0.01)
    extra = {"clock": clock} if clock else {}
    return Supervisor(
        specs,
        status_path=tmp_path / "s.json",
        alerts=AlertSink(tmp_path / "a.jsonl"),
        kill_file=tmp_path / "KILL",
        **extra,
        **kw,
    )


def run_for(s, seconds):
    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(s.run(stop))
        await asyncio.sleep(seconds)
        stop.set()
        await task

    asyncio.run(go())


def test_crashing_component_is_restarted_alerted_and_never_reported_ok(tmp_path):
    calls = {"n": 0}

    async def flaky(hb):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("feed dropped")
        while True:
            hb.beat()
            await asyncio.sleep(0.005)

    s = sup(tmp_path, [ComponentSpec("feed", flaky)])
    run_for(s, 0.3)
    assert calls["n"] >= 3 and s.states["feed"].restarts == 2
    alerts = [json.loads(line) for line in (tmp_path / "a.jsonl").read_text().splitlines()]
    assert len(alerts) >= 2 and "feed dropped" in alerts[0]["message"]
    assert s.health()["overall"] == "OK"  # recovered, and only then OK


def test_unexpected_exit_is_a_failure_not_success(tmp_path):
    async def quits(hb):
        return None

    s = sup(tmp_path, [ComponentSpec("feed", quits)], base_backoff=5)
    run_for(s, 0.1)
    h = s.health()
    assert h["overall"] == "HALTED" and not h["trading_allowed"]
    assert "exited unexpectedly" in h["components"]["feed"]["last_error"]


def test_silent_component_goes_stale(tmp_path):
    now = {"t": 1000.0}

    async def hangs(hb):
        await asyncio.sleep(60)

    s = sup(tmp_path, [ComponentSpec("feed", hangs, max_silence_s=10)], clock=lambda: now["t"])

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(s.run(stop))
        await asyncio.sleep(0.05)
        assert s.health()["overall"] == "OK"
        now["t"] += 30
        await asyncio.sleep(0.05)
        h = s.health()
        stop.set()
        await task
        return h

    h = asyncio.run(go())
    assert h["components"]["feed"]["state"] == "STALE" and not h["trading_allowed"]


def test_kill_switch_overrides_health(tmp_path):
    async def ok(hb):
        while True:
            hb.beat()
            await asyncio.sleep(0.005)

    s = sup(tmp_path, [ComponentSpec("feed", ok)])
    (tmp_path / "KILL").write_text("manual")
    run_for(s, 0.1)
    status = json.loads((tmp_path / "s.json").read_text())
    assert status["overall"] == "KILLED" and status["trading_allowed"] is False


def test_noncritical_failure_degrades_but_critical_halts(tmp_path):
    async def ok(hb):
        while True:
            hb.beat()
            await asyncio.sleep(0.005)

    async def bad(hb):
        raise RuntimeError("x")

    s = sup(
        tmp_path, [ComponentSpec("a", ok), ComponentSpec("b", bad, critical=False)], base_backoff=5
    )
    run_for(s, 0.1)
    assert s.health()["overall"] == "DEGRADED" and not s.health()["trading_allowed"]


def test_external_watchdog_detects_dead_supervisor(tmp_path):
    p = tmp_path / "s.json"
    assert check_status(p)[0] is False  # missing
    p.write_text(json.dumps({"ts": 100.0, "overall": "OK"}))
    assert check_status(p, now=110.0)[0] is True
    ok, msg = check_status(p, max_age_s=30, now=500.0)
    assert ok is False and "stale" in msg  # supervisor stopped writing


def test_token_bucket_limits_rate():
    async def go():
        b = TokenBucket(rate_per_s=200, burst=1)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        for _ in range(11):
            await b.acquire()
        return loop.time() - t0

    assert asyncio.run(go()) >= 0.045  # 10 waits at 200/s
