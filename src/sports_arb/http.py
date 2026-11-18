"""Small asynchronous JSON transport with conservative retries.

Only public, idempotent read endpoints are used by this milestone. The retry
policy must not be copied unchanged to order placement: retries there require
idempotency and order-status reconciliation.
"""

from __future__ import annotations

import asyncio
import logging
import random
from types import TracebackType
from typing import Any

import aiohttp

from sports_arb.errors import ApiError
from sports_arb.ratelimit import TokenBucket

LOGGER = logging.getLogger(__name__)
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class JsonHttpClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 15,
        max_attempts: int = 4,
        limiter: TokenBucket | None = None,
    ) -> None:
        self.limiter = limiter
        self.base_url = base_url.rstrip("/")
        self.max_attempts = max_attempts
        self._timeout = aiohttp.ClientTimeout(
            total=timeout_seconds,
            connect=min(5, timeout_seconds),
            sock_read=timeout_seconds,
        )
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> JsonHttpClient:
        connector = aiohttp.TCPConnector(limit=20, ttl_dns_cache=300)
        self._session = aiohttp.ClientSession(
            connector=connector,
            timeout=self._timeout,
            headers={
                "Accept": "application/json",
                "User-Agent": "sports-arb-scanner/0.1 (public-read-only)",
            },
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
    ) -> Any:
        if self._session is None:
            raise RuntimeError("JsonHttpClient must be used as an async context manager")

        url = f"{self.base_url}{path}"
        last_error: BaseException | None = None
        for attempt in range(1, self.max_attempts + 1):
            if self.limiter is not None:
                await self.limiter.acquire()
            try:
                async with self._session.request(method, url, params=params, json=body) as response:
                    text = await response.text()
                    if response.status in RETRYABLE_STATUS:
                        last_error = ApiError(f"{method} {path} returned HTTP {response.status}")
                        if attempt < self.max_attempts:
                            retry_after = response.headers.get("Retry-After")
                            delay = self._backoff(attempt, retry_after)
                            LOGGER.warning(
                                "Transient HTTP %s from %s; retrying in %.2fs",
                                response.status,
                                path,
                                delay,
                            )
                            await asyncio.sleep(delay)
                            continue
                    if response.status >= 400:
                        raise ApiError(
                            f"{method} {path} returned HTTP {response.status}: {text[:300]!r}"
                        )
                    try:
                        return await response.json()
                    except (aiohttp.ContentTypeError, ValueError) as exc:
                        raise ApiError(
                            f"{method} {path} returned invalid JSON: {text[:300]!r}"
                        ) from exc
            except (TimeoutError, aiohttp.ClientError) as exc:
                last_error = exc
                if attempt < self.max_attempts:
                    delay = self._backoff(attempt, None)
                    LOGGER.warning(
                        "Read request %s failed (%s); retrying in %.2fs",
                        path,
                        type(exc).__name__,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue

        raise ApiError(f"{method} {path} failed after {self.max_attempts} attempts") from last_error

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None) -> float:
        if retry_after is not None:
            try:
                return min(30.0, max(0.0, float(retry_after)))
            except ValueError:
                pass
        exponential = min(8.0, 0.25 * 2 ** (attempt - 1))
        return exponential + random.uniform(0, 0.2)
