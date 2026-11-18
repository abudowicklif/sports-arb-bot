"""Authenticated Kalshi production WebSocket monitoring with gap detection."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp
from kalshi_python_async.auth import KalshiAuth

from sports_arb.kalshi_config import KALSHI_WS_URL, KalshiCredentials

LOGGER = logging.getLogger(__name__)


class KalshiWebSocketStream:
    """Monitor account fills/orders and subscribed books; never invent messages.

    Fresh execution decisions still use REST snapshots. This stream supplies
    authenticated venue evidence and liveness, with sequence gaps treated as a
    hard failure rather than silently continuing on an incomplete view.
    """

    def __init__(self, credentials: KalshiCredentials) -> None:
        self.credentials = credentials
        self.healthy = False
        self._events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._task: asyncio.Task[Any] | None = None
        self._last_seq: dict[int, int] = {}

    async def start(self, tickers: list[str]) -> None:
        auth = KalshiAuth(self.credentials.key_id, self.credentials.private_key_pem)
        headers = auth.create_auth_headers("GET", "/trade-api/ws/v2")
        self._session = aiohttp.ClientSession()
        try:
            self._ws = await self._session.ws_connect(
                KALSHI_WS_URL,
                headers=headers,
                heartbeat=20,
                timeout=aiohttp.ClientWSTimeout(ws_close=5),
            )
            subscriptions = [
                {
                    "id": 1,
                    "cmd": "subscribe",
                    "params": {
                        "channels": ["orderbook_delta"],
                        "market_tickers": tickers,
                        "use_yes_price": True,
                    },
                },
                {"id": 2, "cmd": "subscribe", "params": {"channels": ["fill"]}},
                {"id": 3, "cmd": "subscribe", "params": {"channels": ["user_orders"]}},
            ]
            for command in subscriptions:
                await self._ws.send_json(command)
            confirmed: set[int] = set()
            while len(confirmed) < 3:
                message = await asyncio.wait_for(self._ws.receive_json(), timeout=8)
                if message.get("type") == "error":
                    raise RuntimeError(f"Kalshi WebSocket subscription error: {message}")
                self._track_seq(message)
                if message.get("type") != "subscribed":
                    self._events.put_nowait(message)
                if message.get("type") == "subscribed":
                    sid = int(message.get("sid") or message.get("msg", {}).get("sid") or 0)
                    confirmed.add(sid or len(confirmed) + 1)
            self._events.put_nowait({"type": "authenticated_subscriptions_confirmed", "count": 3})
            self.healthy = True
            self._task = asyncio.create_task(self._run(), name="kalshi-authenticated-ws")
        except Exception:
            await self.stop()
            raise

    async def _run(self) -> None:
        assert self._ws is not None
        try:
            async for message in self._ws:
                if message.type == aiohttp.WSMsgType.TEXT:
                    event = json.loads(message.data)
                    if event.get("type") == "error":
                        raise RuntimeError(f"Kalshi WebSocket error: {event}")
                    self._track_seq(event)
                    self._events.put_nowait(event)
                elif message.type in {
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.ERROR,
                }:
                    raise RuntimeError(f"Kalshi WebSocket closed: {message}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.healthy = False
            self._events.put_nowait({"type": "stream_failure", "error": str(exc)})
            LOGGER.error("Kalshi WebSocket failed: %s", exc)

    def _track_seq(self, event: dict[str, Any]) -> None:
        sid = event.get("sid")
        seq = event.get("seq")
        if isinstance(sid, int) and isinstance(seq, int):
            previous = self._last_seq.get(sid)
            if previous is not None and seq != previous + 1:
                raise RuntimeError(f"Kalshi WebSocket sequence gap sid={sid}: {previous} -> {seq}")
            self._last_seq[sid] = seq

    def drain(self) -> list[dict[str, Any]]:
        result = []
        while not self._events.empty():
            result.append(self._events.get_nowait())
        return result

    async def stop(self) -> None:
        self.healthy = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        if self._session is not None:
            await self._session.close()
            self._session = None
