"""Always-on supervisor: restart with backoff, heartbeat staleness, honest status.

Rules: a component that crashed, exited or went silent is reported as such. The status file
says ``trading_allowed: false`` whenever any critical component is unhealthy or the kill
switch exists. Continuous operation never implies continuous trading.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from sports_arb.live_config import KILL_FILE
from sports_arb.paths import DATA_DIR

LOGGER = logging.getLogger(__name__)
STATUS_PATH = DATA_DIR / "service_status.json"
ALERTS_PATH = DATA_DIR / "alerts.jsonl"


class AlertSink:
    """Log + append-only file. (Webhook/pager delivery needs an endpoint: not configured.)"""

    def __init__(self, path: Path = ALERTS_PATH, clock: Callable[[], float] = time.time) -> None:
        self.path, self.clock = path, clock

    def send(self, severity: str, component: str, message: str) -> None:
        LOGGER.log(
            logging.ERROR if severity != "info" else logging.INFO,
            "[%s] %s: %s",
            severity,
            component,
            message,
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "ts": self.clock(),
                        "severity": severity,
                        "component": component,
                        "message": message,
                    }
                )
                + "\n"
            )


class Heartbeat:
    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock, self.last = clock, clock()

    def beat(self) -> None:
        self.last = self._clock()


@dataclass(slots=True)
class ComponentSpec:
    name: str
    run: Callable[[Heartbeat], Awaitable[None]]
    critical: bool = True
    max_silence_s: float = 60.0


@dataclass(slots=True)
class ComponentState:
    state: str = "STARTING"  # STARTING RUNNING BACKOFF
    restarts: int = 0
    last_error: str = ""
    hb: Heartbeat | None = None
    backoff: float = 0.0
    alerted_stale: bool = False


@dataclass(slots=True)
class Supervisor:
    specs: list[ComponentSpec]
    status_path: Path = STATUS_PATH
    alerts: AlertSink = field(default_factory=AlertSink)
    clock: Callable[[], float] = time.time
    kill_file: Path = KILL_FILE
    base_backoff: float = 1.0
    max_backoff: float = 60.0
    check_interval: float = 2.0
    states: dict[str, ComponentState] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.states = {s.name: ComponentState() for s in self.specs}

    async def _runner(self, spec: ComponentSpec, stop: asyncio.Event) -> None:
        st = self.states[spec.name]
        while not stop.is_set():
            st.hb, st.state = Heartbeat(self.clock), "RUNNING"
            try:
                await spec.run(st.hb)
                st.last_error = "component exited unexpectedly"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                st.last_error = f"{type(exc).__name__}: {exc}"
            st.restarts += 1
            st.backoff = min(self.max_backoff, max(self.base_backoff, st.backoff * 2))
            st.state = "BACKOFF"
            self.alerts.send(
                "error" if spec.critical else "warning",
                spec.name,
                f"{st.last_error}; restart #{st.restarts} in {st.backoff:.1f}s",
            )
            try:
                await asyncio.wait_for(stop.wait(), timeout=st.backoff)
            except TimeoutError:
                pass

    def health(self) -> dict:
        now, comps, overall = self.clock(), {}, "OK"
        for spec in self.specs:
            st = self.states[spec.name]
            silent = st.hb is not None and now - st.hb.last > spec.max_silence_s
            state = "STALE" if (st.state == "RUNNING" and silent) else st.state
            healthy = state == "RUNNING"
            comps[spec.name] = {
                "state": state,
                "critical": spec.critical,
                "restarts": st.restarts,
                "last_error": st.last_error,
                "silent_for_s": round(now - st.hb.last, 1) if st.hb else None,
            }
            if not healthy:
                if spec.critical:
                    overall = "HALTED"
                elif overall == "OK":
                    overall = "DEGRADED"
            if state == "STALE" and not st.alerted_stale:
                st.alerted_stale = True
                self.alerts.send("error", spec.name, f"no heartbeat for {now - st.hb.last:.0f}s")
            if state == "RUNNING":
                st.alerted_stale = False
        killed = self.kill_file.exists()
        return {
            "ts": now,
            "overall": "KILLED" if killed else overall,
            "components": comps,
            "trading_allowed": overall == "OK" and not killed,
        }

    def write_status(self) -> dict:
        status = self.health()
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.status_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(status, indent=2))
        tmp.replace(self.status_path)
        return status

    async def run(self, stop: asyncio.Event) -> None:
        tasks = [asyncio.create_task(self._runner(s, stop), name=s.name) for s in self.specs]
        try:
            while not stop.is_set():
                try:
                    self.write_status()
                except Exception as exc:  # never silently lose the status channel
                    self.alerts.send("error", "supervisor", f"cannot write status: {exc}")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.check_interval)
                except TimeoutError:
                    pass
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def check_status(
    path: Path = STATUS_PATH, max_age_s: float = 30.0, now: float | None = None
) -> tuple[bool, str]:
    """External watchdog: unhealthy if the status file is missing/stale or not OK."""
    if not path.exists():
        return False, "status file missing"
    data = json.loads(path.read_text())
    age = (now if now is not None else time.time()) - data["ts"]
    if age > max_age_s:
        return False, f"status stale ({age:.0f}s): supervisor not running"
    return data["overall"] == "OK", f"overall={data['overall']}"


def main() -> None:
    from sports_arb.record import record_polymarket

    p = argparse.ArgumentParser(prog="sports-arb-service")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="record + monitor 24/7 (no order placement)")
    r.add_argument("--out", type=Path, default=DATA_DIR / "recordings")
    r.add_argument("--interval", type=float, default=2.0)
    r.add_argument("--max-markets", type=int, default=250)
    c = sub.add_parser("check", help="exit 0 only if the running service is healthy")
    c.add_argument("--max-age", type=float, default=30.0)
    a = p.parse_args()
    if a.cmd == "check":
        ok, msg = check_status(max_age_s=a.max_age)
        print(msg)
        sys.exit(0 if ok else 1)

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    async def recorder(hb: Heartbeat) -> None:
        await record_polymarket(
            a.out,
            interval=a.interval,
            max_markets=a.max_markets,
            types=("moneyline", "spreads", "totals"),
            on_cycle=hb.beat,
        )

    async def main_async() -> None:
        stop = asyncio.Event()
        sup = Supervisor(
            [
                ComponentSpec(
                    "polymarket_recorder",
                    recorder,
                    critical=True,
                    max_silence_s=max(30.0, a.interval * 10),
                )
            ]
        )
        try:
            await sup.run(stop)
        except asyncio.CancelledError:
            stop.set()

    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
