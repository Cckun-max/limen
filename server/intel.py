"""Market-intelligence collector and read-only API.

Design notes
------------
* The frontend never talks to LiqFlow; it reads this module's cached snapshots.
* Polling is adaptive.  LiqFlow's own snapshot cadence was measured at roughly
  ten minutes, so polling ``liq-levels`` every five minutes mostly re-downloads
  an unchanged payload.  When ``snapshot_time`` does not advance we back off
  (5 → 10 → 15 minutes).
* Missing data stays missing.  ``overview`` returns ``available: false`` and a
  reason instead of zero-filled buckets.
* Every LiqFlow call increments the local ledger before the request and
  reconciles ``X-RateLimit-Remaining`` afterwards.  A 401 pauses the collector
  instead of retrying; a 429 pauses until ``Retry-After``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import stat
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .hyperliquid_info import HyperliquidError, HyperliquidInfo
from .intel_store import DEFAULT_CAP_BYTES, IntelStore, QuotaExceeded
from .liqflow import LiqFlowClient, LiqFlowError, parse_snapshot_time

LOGGER = logging.getLogger("tide.intel")
router = APIRouter(prefix="/api/v1/intel", tags=["intel"])

MONTHLY_LIMIT = 50_000
HARD_CAP = 45_000
LEVELS_INTERVAL = 300
STATS_INTERVAL = 600
POSITIONS_INTERVAL = 300
DEGRADED_INTERVAL = 900
FILLS_INTERVAL = 180
WATCH_LIMIT = 50
WATCH_SEED = 10
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

ENDPOINTS: dict[str, tuple[str, dict[str, Any] | None]] = {
    "levels": ("/api/aggregate/liq-levels", None),
    "stats": ("/api/stats", None),
    "positions": ("/api/positions/top", {"limit": 25}),
}


def utc_month(ts: float | None = None) -> str:
    moment = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else datetime.now(timezone.utc)
    return moment.strftime("%Y-%m")


def utc_iso(ts: float | None = None) -> str:
    moment = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else datetime.now(timezone.utc)
    return moment.isoformat().replace("+00:00", "Z")


@dataclass
class KeyConfig:
    api_key: str = ""
    source: str = "none"
    path: Path | None = None
    mode: int | None = None
    error: str | None = None


def _chmod_private(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


def load_key_config(data_dir: Path) -> KeyConfig:
    """Resolve the API key: environment wins, then the private JSON file."""
    env_key = (os.environ.get("LIQFLOW_API_KEY") or "").strip()
    if env_key:
        return KeyConfig(api_key=env_key, source="env")
    path = Path(os.environ.get("LIQFLOW_CONFIG") or (Path(data_dir) / "liqflow.json"))
    if not path.exists():
        return KeyConfig(path=path, error="未找到 liqflow.json")
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            _chmod_private(path)
            mode = stat.S_IMODE(path.stat().st_mode)
        raw = json.loads(path.read_text("utf-8"))
        key = str(raw.get("api_key") or raw.get("key") or "").strip()
        if not key:
            return KeyConfig(path=path, mode=mode, error="liqflow.json 缺少 api_key")
        return KeyConfig(api_key=key, source="file", path=path, mode=mode)
    except (OSError, ValueError) as exc:
        return KeyConfig(path=path, error=f"读取 liqflow.json 失败: {type(exc).__name__}")


def classify_fill(fill: dict[str, Any]) -> dict[str, Any] | None:
    """Turn one official fill into an open/add/reduce/close/flip event."""
    try:
        size = abs(float(fill.get("sz") or 0))
        price = float(fill.get("px") or 0)
        start = float(fill.get("startPosition") or 0)
    except (TypeError, ValueError):
        return None
    side_code = str(fill.get("side") or "").strip().upper()
    if side_code in ("B", "BUY", "LONG"):
        delta = size
    elif side_code in ("A", "S", "SELL", "SHORT"):
        delta = -size
    else:
        return None
    if size <= 0:
        return None
    end = start + delta
    if start == 0 and end == 0:
        return None
    if start == 0:
        event = "open"
        direction = "long" if end > 0 else "short"
    elif end == 0:
        event = "close"
        direction = "long" if start > 0 else "short"
    elif (start > 0) != (end > 0):
        event = "flip"
        direction = "long" if end > 0 else "short"
    elif abs(end) > abs(start):
        event = "add"
        direction = "long" if end > 0 else "short"
    else:
        event = "reduce"
        direction = "long" if end > 0 else "short"
    try:
        closed_pnl = float(fill.get("closedPnl") or 0)
    except (TypeError, ValueError):
        closed_pnl = None
    try:
        ts = int(fill.get("time") or 0)
    except (TypeError, ValueError):
        ts = 0
    # Hyperliquid reports milliseconds.  Accept seconds too rather than
    # silently dating an event to 1970 if the upstream unit ever changes.
    if 0 < ts < 10_000_000_000:
        ts *= 1000
    return {
        "coin": str(fill.get("coin") or ""),
        "tid": fill.get("tid"),
        "ts": ts,
        "side": "buy" if delta > 0 else "sell",
        "direction": direction,
        "event": event,
        "size": size,
        "price": price,
        "start_position": start,
        "end_position": end,
        "closed_pnl": closed_pnl,
        "raw_dir": fill.get("dir"),
    }


class IntelService:
    def __init__(self, data_dir: Path, *, live: bool = True, api_key: str | None = None,
                 base_url: str | None = None, cap_bytes: int | None = None,
                 hard_cap: int | None = None, limit: int | None = None,
                 client: LiqFlowClient | None = None, hl_client: HyperliquidInfo | None = None):
        self.data_dir = Path(data_dir)
        self.live = live
        self.monthly_limit = int(limit or os.environ.get("TIDE_INTEL_MONTHLY_LIMIT") or MONTHLY_LIMIT)
        self.hard_cap = int(hard_cap or os.environ.get("TIDE_INTEL_HARD_CAP") or HARD_CAP)
        self.cap_bytes = int(cap_bytes or os.environ.get("TIDE_INTEL_CAP_BYTES")
                             or DEFAULT_CAP_BYTES)
        self.store = IntelStore(self.data_dir, cap_bytes=self.cap_bytes)
        self.config = load_key_config(self.data_dir)
        if api_key:
            self.config = KeyConfig(api_key=api_key, source="explicit")
        self._explicit_key = bool(api_key)
        self._external_client = client is not None
        self.client = client or self._make_client(base_url)
        self.hl = hl_client or HyperliquidInfo()
        self._own_hl = hl_client is None

        self._mode = "normal" if self.config.api_key else "unconfigured"
        self._reasons: list[str] = [] if self.config.api_key else ["no_key"]
        self._last_error: dict[str, Any] | None = None
        self._retry_at = 0.0
        self._failures: dict[str, int] = {}
        self._last_success: dict[str, float] = {}
        self._last_snapshot: dict[str, str | None] = {}
        self._levels_interval = LEVELS_INTERVAL
        self._next_due: dict[str, float] = {}
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._busy = asyncio.Lock()
        self._cache: dict[str, tuple[str, Any]] = {}
        self._key_mtime: float | None = None
        self._last_prune = 0.0
        self._refresh_handle: asyncio.Task | None = None
        if self.config.path and self.config.path.exists():
            self._key_mtime = self.config.path.stat().st_mtime

    # --------------------------------------------------------------- lifecycle
    def _make_client(self, base_url: str | None) -> LiqFlowClient | None:
        if not self.config.api_key:
            return None
        return LiqFlowClient(self.config.api_key, base_url=base_url or os.environ.get(
            "LIQFLOW_BASE_URL", "https://node.liqflow.app"),
            on_attempt=self._quota_attempt)

    async def start(self) -> None:
        if not self.live:
            return
        self._stop.clear()
        self._next_due = {"levels": 0.0, "stats": 0.0, "positions": 0.0, "fills": 0.0}
        self._task = asyncio.create_task(self._run(), name="tide-intel")

    async def stop(self) -> None:
        self._stop.set()
        for task in (self._task, self._refresh_handle):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        if self.client is not None:
            close = getattr(self.client, "aclose", None)
            if close:
                await close()
        if self._own_hl:
            await self.hl.aclose()
        self.store.close()

    def _quota_attempt(self, path: str) -> None:
        self.store.quota_begin(month=utc_month(), endpoint=path,
                               limit=self.monthly_limit, hard_cap=self.hard_cap)

    # ------------------------------------------------------------------- state
    def _set_state(self, mode: str, reason: str | None = None) -> None:
        if mode == "normal":
            self._mode = "normal"
            self._reasons = []
        else:
            self._mode = mode
            if reason and reason not in self._reasons:
                self._reasons.append(reason)

    def _handle_error(self, kind: str, error: LiqFlowError) -> None:
        self._last_error = error.as_dict()
        self._failures[kind] = self._failures.get(kind, 0) + 1
        if error.kind == "auth":
            self._mode = "paused"
            self._reasons = ["unauthorized"]
            self._retry_at = time.time() + 1800
        elif error.kind == "rate_limit":
            self._mode = "paused"
            self._reasons = ["rate_limited"]
            self._retry_at = time.time() + (error.retry_after or 900)
        elif self._failures[kind] >= 3:
            self._mode = "degraded"
            if "upstream_error" not in self._reasons:
                self._reasons.append("upstream_error")
            self._retry_at = time.time() + DEGRADED_INTERVAL

    def _handle_success(self, kind: str) -> None:
        self._failures[kind] = 0
        self._last_success[kind] = time.time()
        self._last_error = None
        if self._mode != "paused":
            self._mode = "normal"
            self._reasons = []

    def projected_monthly_calls(self) -> int:
        """Worst-case LiqFlow calls for 30 days at the base cadence."""
        seconds = 30 * 86400
        return int(seconds / LEVELS_INTERVAL + seconds / STATS_INTERVAL
                   + seconds / POSITIONS_INTERVAL)

    def status(self) -> dict[str, Any]:
        quota = self.store.quota_state(month=utc_month(), limit=self.monthly_limit,
                                       hard_cap=self.hard_cap)
        reasons = list(self._reasons)
        if quota.get("warning") and "quota_warning" not in reasons and self._mode == "normal":
            reasons.append("quota_warning")
        if quota.get("exhausted") and "quota_exhausted" not in reasons:
            reasons.append("quota_exhausted")
        latest = self.store.recent_snapshots("levels", 1)
        snapshot_iso = latest[0].get("snapshot_time") if latest else None
        snapshot_dt = parse_snapshot_time(snapshot_iso)
        now = time.time()
        return {
            "configured": bool(self.config.api_key),
            "key_source": self.config.source,
            "key_file": str(self.config.path) if self.config.path else None,
            "key_mode": ("%o" % self.config.mode) if self.config.mode else None,
            "mode": self._mode,
            "reasons": reasons,
            "last_error": self._last_error,
            "retry_at": self._retry_at or None,
            "quota": quota,
            "storage": self.store.storage_state(),
            "watch_count": len(self.store.watch_list()),
            "last_success": {key: utc_iso(value) for key, value in self._last_success.items()},
            "snapshot_time": snapshot_iso,
            "snapshot_age_seconds": round(now - snapshot_dt.timestamp(), 1) if snapshot_dt else None,
            "levels_interval_seconds": self._levels_interval,
            "projected_monthly_calls": self.projected_monthly_calls(),
            "projected_under_cap": self.projected_monthly_calls() < self.hard_cap,
            "polling": bool(self._task and not self._task.done()),
            "fetched_at": utc_iso(latest[0]["fetched_at"]) if latest else None,
            "updated_at": utc_iso(now),
        }

    # ---------------------------------------------------------------- collection
    async def collect_once(self, kinds: tuple[str, ...] = ("levels", "stats", "positions")) -> dict[str, Any]:
        results: dict[str, Any] = {}
        if not self.config.api_key or self.client is None:
            self._mode = "unconfigured"
            return {"ok": False, "error": "unconfigured", "results": results}
        async with self._busy:
            for kind in kinds:
                if kind not in ENDPOINTS:
                    continue
                if self._mode == "paused" and time.time() < self._retry_at:
                    results[kind] = {"ok": False, "error": "paused"}
                    break
                path, params = ENDPOINTS[kind]
                started = time.time()
                try:
                    payload = await self.client.get(path, params)
                except QuotaExceeded as exc:
                    self._mode = "paused"
                    self._reasons = ["quota_exhausted"]
                    self._retry_at = time.time() + 3600
                    self._last_error = {"kind": "quota", "message": str(exc)}
                    results[kind] = {"ok": False, "error": "quota_exhausted",
                                     "used": exc.used, "cap": exc.cap}
                    break
                except LiqFlowError as exc:
                    self._handle_error(kind, exc)
                    results[kind] = {"ok": False, "error": exc.kind, "status": exc.status}
                    if exc.kind in ("auth", "rate_limit"):
                        break
                    continue

                snapshot_raw = payload.get("snapshot_time") if kind == "levels" else (
                    payload.get("last_snapshot_time") if kind == "stats" else None)
                snapshot_dt = parse_snapshot_time(snapshot_raw)
                snapshot_iso = snapshot_dt.isoformat().replace("+00:00", "Z") if snapshot_dt else None
                meta = self.store.save_snapshot(kind, payload, snapshot_time=snapshot_iso,
                                                fetched_at=int(time.time()))
                self._cache.pop(kind, None)
                previous = self._last_snapshot.get(kind)
                self._last_snapshot[kind] = snapshot_iso
                self._handle_success(kind)
                rate = self.client.last_rate_limit
                self.store.quota_record(month=utc_month(), endpoint=path, status=200,
                                        provider_limit=rate.limit,
                                        provider_remaining=rate.remaining)
                if kind == "levels":
                    self._adapt_levels_interval(previous, snapshot_iso)
                if kind == "positions":
                    self._seed_watchlist(payload)
                results[kind] = {"ok": True, "snapshot_time": snapshot_iso,
                                 "stored_bytes": meta["stored_bytes"],
                                 "elapsed_ms": int((time.time() - started) * 1000)}

            self._maybe_prune()
        return {"ok": any(item.get("ok") for item in results.values()), "results": results}

    def _adapt_levels_interval(self, previous: str | None, snapshot_iso: str | None) -> None:
        if snapshot_iso and previous and snapshot_iso == previous:
            self._levels_interval = min(600, self._levels_interval + 300)
            LOGGER.info("LiqFlow snapshot unchanged; backing off to %ss", self._levels_interval)
        else:
            self._levels_interval = LEVELS_INTERVAL

    def _seed_watchlist(self, payload: dict[str, Any]) -> None:
        if self.store.meta_get("watch_seeded"):
            return
        if self.store.watch_list():
            self.store.meta_set("watch_seeded", "1")
            return
        items = payload.get("positions") or []
        added = 0
        for item in items[:WATCH_SEED]:
            address = str(item.get("address") or "")
            if not ADDRESS_RE.match(address):
                continue
            if self.store.watch_add(address, label=item.get("cohort_label"),
                                    source="top_positions"):
                added += 1
        if added:
            self.store.meta_set("watch_seeded", "1")
            LOGGER.info("seeded %s addresses from the positions leaderboard", added)

    def _maybe_prune(self) -> None:
        now = time.time()
        if now - self._last_prune < 3600:
            return
        self._last_prune = now
        try:
            report = self.store.prune(cap_bytes=self.cap_bytes)
            if report["removed_files"]:
                LOGGER.info("intel storage pruned: %s", report)
        except OSError as exc:
            LOGGER.warning("intel storage prune failed: %s", exc)

    # -------------------------------------------------------------------- fills
    async def _poll_fills(self) -> dict[str, Any]:
        watch = self.store.watch_list()[:WATCH_LIMIT]
        inserted = 0
        errors = 0
        for entry in watch:
            address = entry["address"]
            try:
                start = self.store.event_ts(address)
                fills = await self.hl.user_fills(address, start + 1 if start else None)
                if not start:
                    fills = fills[:30]
            except HyperliquidError as exc:
                errors += 1
                LOGGER.warning("userFills failed for %s: %s", address[:10], exc)
                continue
            events = [event for event in (classify_fill(fill) for fill in fills) if event]
            inserted += self.store.add_events(address, events)
            newest = max((event["ts"] for event in events), default=0)
            if newest > start:
                self.store.event_mark(address, newest)
        if inserted:
            LOGGER.info("stored %s new large-account events", inserted)
        return {"watched": len(watch), "inserted": inserted, "errors": errors}

    def _fills_interval(self) -> int:
        count = len(self.store.watch_list())
        if count <= 10:
            return FILLS_INTERVAL
        if count <= 30:
            return 300
        return 600

    async def refresh_now(self) -> None:
        await self.collect_once(("levels", "positions", "stats"))

    def request_refresh(self) -> bool:
        if self._refresh_handle and not self._refresh_handle.done():
            return False
        self._refresh_handle = asyncio.create_task(self.refresh_now())
        return True

    # ------------------------------------------------------------------ watchlist
    def watchlist(self) -> list[dict[str, Any]]:
        return self.store.watch_list()

    def add_watch(self, address: str, label: str | None = None) -> dict[str, Any]:
        address = (address or "").strip()
        if not ADDRESS_RE.match(address):
            raise ValueError("地址格式不正确（需要 0x 开头的 40 位十六进制）")
        if len(self.store.watch_list()) >= WATCH_LIMIT and not any(
                entry["address"].lower() == address.lower() for entry in self.store.watch_list()):
            raise ValueError(f"最多跟踪 {WATCH_LIMIT} 个地址")
        added = self.store.watch_add(address, label=label, source="manual")
        self._next_due["fills"] = 0.0
        return {"address": address.lower(), "added": added,
                "count": len(self.store.watch_list())}

    def remove_watch(self, address: str) -> dict[str, Any]:
        removed = self.store.watch_remove(address)
        return {"address": address.lower(), "removed": removed,
                "count": len(self.store.watch_list())}

    # ---------------------------------------------------------------- read model
    def _snapshot(self, kind: str) -> dict[str, Any] | None:
        latest = self.store.recent_snapshots(kind, 1)
        if not latest:
            return None
        row = latest[0]
        cached = self._cache.get(kind)
        if cached and cached[0] == row["rel_path"]:
            return {"payload": cached[1], "meta": row}
        loaded = self.store.latest_snapshot(kind)
        if loaded:
            self._cache[kind] = (row["rel_path"], loaded["payload"])
            return loaded
        return None

    def overview(self, symbol: str) -> dict[str, Any]:
        symbol = (symbol or "BTC").strip().upper()
        state = self.status()
        coins: list[dict[str, Any]] = []
        snapshot_iso = None
        fetched_at = None
        levels_meta: dict[str, Any] | None = None
        if not symbol.startswith("IBKR:"):
            levels = self._snapshot("levels")
            if levels:
                payload = levels["payload"]
                meta = levels["meta"]
                snapshot_iso = meta.get("snapshot_time")
                fetched_at = utc_iso(meta["fetched_at"])
                merged: dict[float, dict[str, Any]] = {}
                for row in payload.get("levels") or []:
                    if str(row.get("coin") or "").upper() != symbol:
                        continue
                    try:
                        price = float(row["liq_price"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    bucket = merged.setdefault(price, {"price": price, "long": None, "short": None})
                    for key in ("long_notional", "short_notional"):
                        if row.get(key) is not None:
                            try:
                                value = float(row[key])
                            except (TypeError, ValueError):
                                continue
                            side = "long" if key == "long_notional" else "short"
                            bucket[side] = (bucket[side] or 0.0) + value
                coins = [bucket for _, bucket in sorted(merged.items())
                         if bucket["long"] is not None or bucket["short"] is not None]
                levels_meta = meta

        positions_payload = self._snapshot("positions")
        positions = []
        positions_fetched = None
        if positions_payload:
            positions = positions_payload["payload"].get("positions") or []
            positions_fetched = utc_iso(positions_payload["meta"]["fetched_at"])

        tracked = {entry["address"].lower() for entry in self.store.watch_list()}
        for item in positions:
            item["tracked"] = str(item.get("address") or "").lower() in tracked

        coverage = self.store.coverage_effective(symbol) if not symbol.startswith("IBKR:") else {
            "coin": symbol, "status": "unverified", "note": "IBKR 标的不在 LiqFlow 覆盖范围"}

        available = bool(coins)
        reason = None
        if symbol.startswith("IBKR:"):
            reason = "该标的是 IBKR 行情，LiqFlow 只覆盖 Hyperliquid 永续"
        elif levels is None:
            reason = "尚无 LiqFlow 快照"
        elif not coins:
            reason = "LiqFlow 未覆盖该标的"

        return {
            "symbol": symbol,
            "available": available,
            "reason": reason,
            "source": "LiqFlow",
            "snapshot_time": snapshot_iso,
            "fetched_at": fetched_at,
            "levels": coins,
            "levels_meta": ({key: levels_meta.get(key) for key in
                             ("snapshot_time", "fetched_at", "raw_bytes", "stored_bytes")}
                            if levels_meta else None),
            "coverage": coverage,
            "positions": positions,
            "positions_fetched_at": positions_fetched,
            "events": self.store.events(50),
            "watchlist": sorted(tracked),
            "status": state,
        }

    # --------------------------------------------------------------- background
    async def _maybe_reload_key(self) -> None:
        if self._explicit_key or self._external_client:
            return
        path = self.config.path
        if not path:
            return
        try:
            mtime = path.stat().st_mtime if path.exists() else None
        except OSError:
            mtime = None
        if mtime == self._key_mtime:
            return
        self._key_mtime = mtime
        fresh = load_key_config(self.data_dir)
        if not fresh.api_key:
            self.config = fresh
            if self.client:
                await self.client.aclose()
                self.client = None
            self._mode = "unconfigured"
            self._reasons = ["no_key"]
            return
        old = self.client
        self.config = fresh
        self.client = self._make_client(None)
        if old:
            await old.aclose()
        self._mode = "normal"
        self._reasons = []
        self._retry_at = 0
        LOGGER.info("LiqFlow key reloaded from %s", path)

    async def _run(self) -> None:
        try:
            while not self._stop.is_set():
                await self._maybe_reload_key()
                if not self.config.api_key:
                    await self._sleep(30)
                    continue
                now = time.time()
                if self._mode == "paused":
                    if now < self._retry_at:
                        await self._sleep(min(30, max(1, self._retry_at - now)))
                        continue
                    if "unauthorized" in self._reasons:
                        await self._sleep(30)
                        continue
                    self._mode = "degraded"
                due = [kind for kind, interval in (
                    ("levels", self._levels_interval),
                    ("stats", STATS_INTERVAL),
                    ("positions", POSITIONS_INTERVAL))
                    if now >= self._next_due.get(kind, 0)]
                if due:
                    await self.collect_once(tuple(due))
                    stamp = time.time()
                    for kind in due:
                        base = {"levels": self._levels_interval, "stats": STATS_INTERVAL,
                                "positions": POSITIONS_INTERVAL}[kind]
                        if self._mode in ("degraded", "paused"):
                            base = max(base, DEGRADED_INTERVAL)
                        self._next_due[kind] = stamp + base
                if time.time() >= self._next_due.get("fills", 0):
                    try:
                        await self._poll_fills()
                    except Exception:  # never let tracked-address errors kill the loop
                        LOGGER.exception("fills poll failed")
                    self._next_due["fills"] = time.time() + self._fills_interval()
                await self._sleep(5)
        except asyncio.CancelledError:
            raise

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass


# --------------------------------------------------------------------- API
class WatchRequest(BaseModel):
    address: str
    label: str | None = None


def _require_local(request: Request) -> None:
    if request.headers.get("x-local-market") != "1":
        raise HTTPException(403, "Local request header required")
    origin = request.headers.get("origin")
    if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
        raise HTTPException(403, "Same-origin requests only")


def _service(request: Request) -> IntelService:
    service = getattr(request.app.state, "intel", None)
    if service is None:
        raise HTTPException(503, "市场情报服务未启动")
    return service


@router.get("/status")
async def intel_status(request: Request) -> dict[str, Any]:
    return _service(request).status()


@router.get("/overview")
async def intel_overview(request: Request, symbol: str = "BTC") -> dict[str, Any]:
    return _service(request).overview(symbol)


@router.get("/events")
async def intel_events(request: Request, limit: int = 50) -> dict[str, Any]:
    service = _service(request)
    limit = max(1, min(200, int(limit)))
    return {"events": service.store.events(limit)}


@router.get("/watchlist")
async def intel_watchlist(request: Request) -> dict[str, Any]:
    return {"addresses": _service(request).watchlist()}


@router.post("/watchlist")
async def intel_watch_add(request: Request, body: WatchRequest) -> dict[str, Any]:
    _require_local(request)
    try:
        return _service(request).add_watch(body.address, body.label)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.delete("/watchlist/{address}")
async def intel_watch_remove(request: Request, address: str) -> dict[str, Any]:
    _require_local(request)
    return _service(request).remove_watch(address)


@router.post("/refresh", status_code=202)
async def intel_refresh(request: Request) -> dict[str, Any]:
    _require_local(request)
    service = _service(request)
    if not service.config.api_key:
        raise HTTPException(409, "尚未配置 LiqFlow key")
    accepted = service.request_refresh()
    return {"status": "scheduled" if accepted else "already_running"}
