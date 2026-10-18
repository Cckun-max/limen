"""LiqFlow HTTP client.

Free-tier constraints that shaped this module:

* every request shares one 50,000/month bucket, so callers must reserve quota
  *before* the request (see ``on_attempt``) and requests are serialised with a
  >= 3s gap;
* the free key expires in stages during late October 2026: a missing/invalid
  key must surface as :class:`LiqFlowError` with ``kind == "auth"`` so the
  collector can pause instead of hammering the endpoint;
* rate-limit headers are only present when authenticated, therefore they are
  advisory: the local ledger stays authoritative.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

DEFAULT_BASE_URL = "https://node.liqflow.app"
USER_AGENT = "limen-intel/0.1 (personal use)"


class LiqFlowError(RuntimeError):
    """A classified LiqFlow failure.

    ``kind`` is one of: auth, rate_limit, timeout, network, server, client,
    payload.  The collector switches state based on it; nothing else should
    need to inspect the raw httpx exception.
    """

    def __init__(self, kind: str, message: str, *, status: int | None = None,
                 retry_after: float | None = None):
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.message = message
        self.status = status
        self.retry_after = retry_after

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "message": self.message, "status": self.status,
                "retry_after": self.retry_after}


@dataclass(frozen=True)
class RateLimit:
    limit: int | None = None
    remaining: int | None = None
    period: str | None = None
    observed_at: int = 0


def _as_int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def parse_snapshot_time(value: Any) -> datetime | None:
    """Parse LiqFlow timestamps.

    ``/api/aggregate/liq-levels`` returns ``...260959Z`` while ``/api/stats``
    returns the same instant without a timezone (``...260959``).  A naive
    value is UTC; treating it as local time would shift every freshness badge
    by the machine's UTC offset.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    text = str(value).strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class LiqFlowClient:
    def __init__(self, api_key: str, *, base_url: str = DEFAULT_BASE_URL,
                 timeout: float = 20.0, min_interval: float = 3.0,
                 transport: httpx.AsyncBaseTransport | None = None,
                 auth_style: str = "bearer",
                 on_attempt: Callable[[str], None] | None = None):
        self.api_key = (api_key or "").strip()
        self.base_url = base_url.rstrip("/")
        self.min_interval = max(0.0, min_interval)
        self.auth_style = auth_style
        self.on_attempt = on_attempt
        self.calls = 0
        self.last_rate_limit = RateLimit()
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout,
                                         transport=transport, headers=headers,
                                         follow_redirects=False)
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

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            return {}
        if self.auth_style == "x-api-key":
            return {"X-API-Key": self.api_key}
        return {"Authorization": f"Bearer {self.api_key}"}

    @staticmethod
    def _rate_limit(response: httpx.Response) -> RateLimit:
        headers = {key.lower(): value for key, value in response.headers.items()}
        return RateLimit(
            limit=_as_int(headers.get("x-ratelimit-limit")),
            remaining=_as_int(headers.get("x-ratelimit-remaining")),
            period=headers.get("x-ratelimit-period"),
            observed_at=int(time.time()),
        )

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        raw = response.headers.get("retry-after")
        if not raw:
            return None
        try:
            return max(0.0, float(raw))
        except ValueError:
            return None

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET one endpoint, retrying once on transient failures.

        Retries do not happen for 401/403/429: those have to reach the state
        machine immediately.  ``on_attempt`` runs before every HTTP attempt,
        so the local quota ledger counts retries too (the provider does).
        """
        last_error: LiqFlowError | None = None
        for attempt in range(2):
            await self._throttle()
            if self.on_attempt is not None:
                self.on_attempt(path)
            try:
                response = await self._client.get(path, params=params, headers=self._headers())
            except httpx.TimeoutException as exc:
                last_error = LiqFlowError("timeout", f"{type(exc).__name__}: request timed out")
                if attempt == 0:
                    await asyncio.sleep(3)
                    continue
                raise last_error from exc
            except httpx.TransportError as exc:
                last_error = LiqFlowError("network", f"{type(exc).__name__}: {exc}")
                if attempt == 0:
                    await asyncio.sleep(3)
                    continue
                raise last_error from exc

            self.calls += 1
            self.last_rate_limit = self._rate_limit(response)
            status = response.status_code
            if status in (401, 403):
                raise LiqFlowError("auth", "LiqFlow rejected the API key", status=status)
            if status == 429:
                raise LiqFlowError("rate_limit", "LiqFlow quota exhausted", status=status,
                                   retry_after=self._retry_after(response))
            if status >= 500:
                last_error = LiqFlowError("server", f"LiqFlow returned {status}", status=status)
                if attempt == 0:
                    await asyncio.sleep(3)
                    continue
                raise last_error
            if status >= 400:
                raise LiqFlowError("client", f"LiqFlow returned {status}", status=status)
            try:
                return response.json()
            except ValueError as exc:
                raise LiqFlowError("payload", "LiqFlow returned invalid JSON", status=status) from exc
        raise last_error or LiqFlowError("network", "LiqFlow request failed")
