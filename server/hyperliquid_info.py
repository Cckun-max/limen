"""Minimal official Hyperliquid ``/info`` client.

Used for two things only: sampling real liquidation prices for verification and
polling fills for a short watchlist.  It is deliberately separate from the
market websocket so a failure here can never disturb the existing quote feed.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://api.hyperliquid.xyz"
USER_AGENT = "limen-intel/0.1 (personal use)"


class HyperliquidError(RuntimeError):
    def __init__(self, kind: str, message: str, *, status: int | None = None):
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.message = message
        self.status = status

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "message": self.message, "status": self.status}


class HyperliquidInfo:
    def __init__(self, *, base_url: str = DEFAULT_BASE_URL, timeout: float = 20.0,
                 min_interval: float = 0.25,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.min_interval = max(0.0, min_interval)
        self.calls = 0
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout,
                                         transport=transport,
                                         headers={"User-Agent": USER_AGENT,
                                                  "Content-Type": "application/json"})
        self._lock = asyncio.Lock()
        self._last_request = 0.0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _throttle(self) -> None:
        if not self.min_interval:
            return
        async with self._lock:
            delay = self.min_interval - (time.monotonic() - self._last_request)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_request = time.monotonic()

    async def post(self, body: dict[str, Any]) -> Any:
        last_error: HyperliquidError | None = None
        for attempt in range(2):
            await self._throttle()
            try:
                response = await self._client.post("/info", json=body)
            except httpx.TimeoutException as exc:
                last_error = HyperliquidError("timeout", f"{type(exc).__name__}: request timed out")
                if attempt == 0:
                    await asyncio.sleep(2)
                    continue
                raise last_error from exc
            except httpx.TransportError as exc:
                last_error = HyperliquidError("network", f"{type(exc).__name__}: {exc}")
                if attempt == 0:
                    await asyncio.sleep(2)
                    continue
                raise last_error from exc

            self.calls += 1
            status = response.status_code
            if status == 429:
                raise HyperliquidError("rate_limit", "Hyperliquid rate limit", status=status)
            if status >= 500:
                last_error = HyperliquidError("server", f"Hyperliquid returned {status}", status=status)
                if attempt == 0:
                    await asyncio.sleep(2)
                    continue
                raise last_error
            if status >= 400:
                raise HyperliquidError("client", f"Hyperliquid returned {status}", status=status)
            try:
                return response.json()
            except ValueError as exc:
                raise HyperliquidError("payload", "Hyperliquid returned invalid JSON") from exc
        raise last_error or HyperliquidError("network", "Hyperliquid request failed")

    async def clearinghouse_state(self, address: str) -> dict[str, Any]:
        payload = await self.post({"type": "clearinghouseState", "user": address})
        if not isinstance(payload, dict):
            raise HyperliquidError("payload", "clearinghouseState returned a non-object")
        return payload

    async def user_fills(self, address: str, start_time: int | None = None) -> list[dict[str, Any]]:
        if start_time:
            payload = await self.post({"type": "userFillsByTime", "user": address,
                                       "startTime": int(start_time), "aggregateByTime": False})
        else:
            payload = await self.post({"type": "userFills", "user": address})
        if payload is None:
            return []
        if not isinstance(payload, list):
            raise HyperliquidError("payload", "userFills returned a non-list")
        return [row for row in payload if isinstance(row, dict)]
