import asyncio
import json
from types import SimpleNamespace

import aiohttp

from sports_arb.kalshi_stream import KalshiWebSocketStream


class FakeWs:
    def __init__(self, payloads):
        self.payloads = payloads

    def __aiter__(self):
        async def gen():
            for p in self.payloads:
                yield SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(p))

        return gen()


def make(payloads):
    s = KalshiWebSocketStream(SimpleNamespace(key_id="k", private_key_pem="p"))
    s._ws = FakeWs(payloads)
    s.healthy = True
    return s


def test_events_are_actually_queued_regression():
    """Un-awaited Queue.put used to drop every event."""
    s = make([{"type": "fill", "sid": 2, "seq": 1}, {"type": "fill", "sid": 2, "seq": 2}])
    asyncio.run(s._run())
    types = [e["type"] for e in s.drain()]
    assert types == ["fill", "fill"]


def test_sequence_gap_marks_unhealthy_and_records_failure():
    s = make([{"type": "d", "sid": 1, "seq": 1}, {"type": "d", "sid": 1, "seq": 3}])
    asyncio.run(s._run())
    assert s.healthy is False
    events = s.drain()
    assert events[-1]["type"] == "stream_failure" and "gap" in events[-1]["error"]


def test_server_error_message_fails_closed():
    s = make([{"type": "error", "msg": "boom"}])
    asyncio.run(s._run())
    assert s.healthy is False
    assert s.drain()[-1]["type"] == "stream_failure"
