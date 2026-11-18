"""Authenticated production WebSocket for real Polymarket order/trade updates."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

import aiohttp

from sports_arb.live_config import Credentials

LOGGER = logging.getLogger(__name__)
USER_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"


class AuthenticatedUserStream:
    def __init__(self, credentials: Credentials) -> None:
        self.credentials = credentials
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        self.healthy = False
        self.error: str | None = None
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._tasks: list[asyncio.Task[Any]] = []

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(
            headers={"User-Agent": "sports-arb-live/1.0"},
            timeout=aiohttp.ClientTimeout(total=30),
        )
        self._ws = await self._session.ws_connect(USER_WS_URL, autoping=False, heartbeat=None)
        await self._ws.send_json(
            {
                "auth": {
                    "apiKey": self.credentials.api_key,
                    "secret": self.credentials.api_secret,
                    "passphrase": self.credentials.api_passphrase,
                },
                "type": "user",
            }
        )
        self.healthy = True
        self._tasks = [
            asyncio.create_task(self._receive_loop(), name="polymarket-user-ws-rx"),
            asyncio.create_task(self._ping_loop(), name="polymarket-user-ws-ping"),
        ]
        # Authentication is asynchronous. Execution still reconciles every fill
        # through authenticated REST; the stream is a low-latency secondary feed.
        await asyncio.sleep(0.25)

    async def stop(self) -> None:
        self.healthy = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        if self._session is not None:
            await self._session.close()
            self._session = None

    def drain(self) -> list[dict[str, Any]]:
        events = []
        while True:
            try:
                events.append(self.events.get_nowait())
            except asyncio.QueueEmpty:
                return events

    async def _ping_loop(self) -> None:
        try:
            while self.healthy and self._ws is not None:
                await asyncio.sleep(10)
                await self._ws.send_str("PING")
        except Exception as exc:
            self._mark_failed(exc)

    async def _receive_loop(self) -> None:
        try:
            assert self._ws is not None
            async for message in self._ws:
                if message.type == aiohttp.WSMsgType.TEXT:
                    if message.data in ("PONG", "PING", "{}", ""):
                        continue
                    payload = json.loads(message.data)
                    events = payload if isinstance(payload, list) else [payload]
                    for event in events:
                        if isinstance(event, dict):
                            if self.events.full():
                                raise RuntimeError("user event queue overflow")
                            self.events.put_nowait(event)
                elif message.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.ERROR,
                ):
                    raise ConnectionError(f"user WebSocket closed: {message.type}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._mark_failed(exc)

    def _mark_failed(self, exc: Exception) -> None:
        self.healthy = False
        self.error = f"{type(exc).__name__}: {exc}"
        LOGGER.error("Authenticated user stream failed: %s", self.error)
