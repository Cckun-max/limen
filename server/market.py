import asyncio
import contextlib
import json
import logging
import math
import random

import httpx
from websockets.asyncio.client import connect

from .indicators import VERSION, calculate
from .models import CONFIRM_DELAY_MS, TIMEFRAMES, missing_ranges, now_ms, parse_candle
from .storage import Store

log = logging.getLogger("local_market")
INFO_URL = "https://api.hyperliquid.xyz/info"
WS_URL = "wss://api.hyperliquid.xyz/ws"


class MarketService:
    def __init__(self, store: Store, symbol: str = "BTC"):
        self.symbol = symbol
        self.instrument_id = f"hyperliquid:perp:{symbol}"
        self.info_provider = None
        self.metadata_time = None
        self.store = store
        self.quote = store.get(f"quote:{symbol}") or (store.get("btc_quote") if symbol == "BTC" else None)
        self.revisions = {tf: 0 for tf in TIMEFRAMES}
        self.last_rest = {tf: 0 for tf in TIMEFRAMES}
        self.last_candle = {tf: 0 for tf in TIMEFRAMES}
        self.needs_reconcile = {tf: True for tf in TIMEFRAMES}
        self.errors: dict[str, str] = {}
        self.ws_connected = False
        self.last_ws = 0
        self.syncing = False
        self.sync_event = asyncio.Event()
        self.sync_lock = asyncio.Lock()
        self.tasks = []
        self.http: httpx.AsyncClient | None = None
        self.last_sync_attempt = 0
        self.reconnects = 0
        self.feature_cache = {}
        self.history_loaded: set[str] = set()

    async def start(self):
        self.http = httpx.AsyncClient(timeout=12, headers={"User-Agent": "tide/0.1"})
        self.tasks = [asyncio.create_task(self._rest_loop()), asyncio.create_task(self._ws_loop())]

    async def stop(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.http:
            await self.http.aclose()

    async def _info(self, body: dict):
        if self.info_provider:
            return await self.info_provider(body)
        assert self.http is not None
        response = await self.http.post(INFO_URL, json=body)
        response.raise_for_status()
        return response.json()

    def _update_quote(self, ctx: dict, source: str, received_at: int | None = None):
        received_at = now_ms() if received_at is None else received_at
        if self.quote and received_at < self.quote["received_at"]:
            return
        def number(key):
            value = float(ctx[key]) if ctx.get(key) is not None else None
            if value is not None and not math.isfinite(value):
                raise ValueError("Invalid quote")
            return value
        mark, previous = number("markPx"), number("prevDayPx")
        if not mark or not previous:
            raise ValueError("Missing quote price")
        self.quote = {"instrument_id": self.instrument_id, "symbol": self.symbol, "mark": mark,
                      "mid": number("midPx"), "previous_day": previous,
                      "change_pct": (mark / previous - 1) * 100,
                      "volume_usd": number("dayNtlVlm"), "oi_base": number("openInterest"),
                      "funding": number("funding"), "received_at": received_at,
                      "source_time": None, "source": source, "price_kind": "mark"}
        self.store.put(f"quote:{self.symbol}", self.quote)

    def features(self, timeframe):
        key = self.revisions[timeframe]
        old = self.feature_cache.get(timeframe)
        if old and old[0] == key:
            return old[1], old[2]
        bars = self.store.bars(timeframe, symbol=self.symbol)
        values = calculate(bars)
        self.feature_cache[timeframe] = (key, bars, values)
        return bars, values

    def chart(self, timeframe):
        bars, values = self.features(timeframe)
        return {"instrument_id": self.instrument_id, "symbol": self.symbol, "timeframe": timeframe,
                "bars": [bar.public() for bar in bars[-800:]], "indicators": values[-800:],
                "algorithm_version": VERSION, "revision": self.revisions[timeframe],
                "coverage_start": bars[0].time if bars else None,
                "quality": self._quality(timeframe, bars), "as_of": now_ms()}

    def _quality(self, timeframe, bars):
        at = now_ms()
        gaps = missing_ranges(bars, timeframe, at)
        pending = sum(b.status != "confirmed" and b.end <= at - CONFIRM_DELAY_MS for b in bars)
        fresh = (self.ws_connected and at - self.last_candle[timeframe] < 45_000) or at - self.last_rest[timeframe] < 65_000
        status = "warming" if not bars else "stale" if not fresh else "gap" if gaps else "recovering" if self.needs_reconcile[timeframe] else "pending" if pending else "fresh"
        confirmed = next((b for b in reversed(bars) if b.status == "confirmed"), None)
        return {"status": status, "gaps": gaps, "pending": pending, "count": len(bars),
                "last_confirmed_end": confirmed.end if confirmed else None,
                "last_rest": self.last_rest[timeframe] or None,
                "error": self.errors.get(timeframe)}

    def overview(self):
        at = now_ms()
        summaries = {}
        for tf in TIMEFRAMES:
            bars, values = self.features(tf)
            confirmed = next((v for v in reversed(values) if not v["provisional"]), None)
            summaries[tf] = {"revision": self.revisions[tf], "quality": self._quality(tf, bars),
                             "confirmed_indicators": confirmed,
                             "last_close": next((b.close for b in reversed(bars) if b.status == "confirmed"), None)}
        quote_age = at - self.quote["received_at"] if self.quote else None
        mode = "live" if self.ws_connected and at - self.last_ws < 45_000 else "polling" if any(at - t < 65_000 for t in self.last_rest.values()) else "offline"
        return {"instrument_id": self.instrument_id, "symbol": self.symbol, "quote": self.quote, "quote_stale": quote_age is None or quote_age > 65_000,
                "transport": mode, "ws_connected": self.ws_connected, "last_ws": self.last_ws or None,
                "syncing": self.syncing, "reconnects": self.reconnects, "timeframes": summaries,
                "errors": self.errors.copy(), "server_time": at, "algorithm_version": VERSION}

    async def reconcile(self):
        async with self.sync_lock:
            self.syncing = True
            self.last_sync_attempt = now_ms()
            try:
                for tf, step in TIMEFRAMES.items():
                    requested_at = now_ms()
                    bars = self.store.bars(tf, symbol=self.symbol)
                    gaps = missing_ranges(bars, tf, requested_at)
                    # Weekly history can contain fewer than 300 bars. Bootstrap once per
                    # process, then reconcile the tail rather than refetch all years every time.
                    if len(bars) < 300 and tf not in self.history_loaded:
                        start = requested_at - 400 * step
                    else:
                        confirmed = [b for b in bars if b.status == "confirmed"]
                        start = confirmed[-1].time - step if confirmed else requested_at - 400 * step
                        unfinished = [b.time for b in bars if b.status != "confirmed"]
                        if unfinished:
                            start = min(start, min(unfinished))
                        if gaps:
                            start = min(start, gaps[0]["start"])
                    start = max(start, requested_at - 4_999 * step)
                    try:
                        raw = await self._info({"type": "candleSnapshot", "req": {"coin": self.symbol, "interval": tf,
                                               "startTime": start, "endTime": requested_at}})
                        if not isinstance(raw, list) or not raw:
                            raise ValueError("行情源未返回 K 线")
                        received = now_ms()
                        parsed = [parse_candle(b, tf, received, source="rest", requested_at=requested_at, symbol=self.symbol) for b in raw]
                        if self.store.merge(parsed):
                            self.revisions[tf] += 1
                        self.last_rest[tf] = received
                        self.history_loaded.add(tf)
                        self.needs_reconcile[tf] = False
                        self.errors.pop(tf, None)
                    except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
                        self.errors[tf] = self._error_text(error)
                        log.warning("%s sync: %s", tf, error)
                    await asyncio.sleep(0.15)
                try:
                    metadata, contexts = await self._info({"type": "metaAndAssetCtxs"})
                    index = next(i for i, asset in enumerate(metadata["universe"]) if asset["name"] == self.symbol)
                    self._update_quote(contexts[index], "rest", self.metadata_time() if self.metadata_time else None)
                    self.errors.pop("quote", None)
                except (httpx.HTTPError, ValueError, KeyError, TypeError, StopIteration) as error:
                    self.errors["quote"] = self._error_text(error)
            finally:
                self.syncing = False

    @staticmethod
    def _error_text(error):
        if isinstance(error, httpx.HTTPStatusError):
            return f"行情接口 HTTP {error.response.status_code}，稍后重试"
        return "行情连接暂时不可用，正在自动重试" if isinstance(error, httpx.HTTPError) else str(error)[:160]

    async def _rest_loop(self):
        while True:
            self.sync_event.clear()
            try:
                await self.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Reconciliation failed")
            try:
                await asyncio.wait_for(self.sync_event.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass
            # Coalesce reconnect/button requests and stay comfortably under the public rate limit.
            elapsed = (now_ms() - self.last_sync_attempt) / 1000
            if elapsed < 5:
                await asyncio.sleep(5 - elapsed)

    async def _ping(self, ws):
        while True:
            await asyncio.sleep(20)
            await ws.send(json.dumps({"method": "ping"}))

    async def _ws_loop(self):
        failures = 0
        while True:
            ping_task = None
            connected_at = now_ms()
            try:
                async with connect(WS_URL, open_timeout=12, close_timeout=3, ping_interval=None, max_queue=256) as ws:
                    self.ws_connected = True
                    self.last_ws = now_ms()
                    self.errors.pop("websocket", None)
                    for tf in TIMEFRAMES:
                        await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "candle", "coin": self.symbol, "interval": tf}}))
                    await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "activeAssetCtx", "coin": self.symbol}}))
                    self.sync_event.set()
                    ping_task = asyncio.create_task(self._ping(ws))
                    while True:
                        raw = await asyncio.wait_for(ws.recv(), timeout=45)
                        event = json.loads(raw)
                        self.last_ws = now_ms()
                        if event.get("channel") == "candle":
                            data = event["data"]
                            tf = data.get("i")
                            if tf in TIMEFRAMES:
                                bar = parse_candle(data, tf, now_ms(), source="ws", symbol=self.symbol)
                                self.last_candle[tf] = bar.received_at
                                if self.store.merge([bar]):
                                    self.revisions[tf] += 1
                        elif event.get("channel") == "activeAssetCtx" and event["data"].get("coin") == self.symbol:
                            self._update_quote(event["data"]["ctx"], "ws")
                        elif event.get("channel") == "error":
                            raise ValueError("行情订阅未成功，正在重新连接")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.warning("WebSocket disconnected: %s", error)
                self.errors["websocket"] = "实时连接中断，正在重连；可用时以 REST 补数"
            finally:
                self.ws_connected = False
                self.needs_reconcile = {tf: True for tf in TIMEFRAMES}
                if ping_task:
                    ping_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await ping_task
            failures = 0 if now_ms() - connected_at > 60_000 else failures + 1
            self.reconnects += 1
            self.sync_event.set()
            await asyncio.sleep(min(30, 2 ** min(failures, 5)) + random.random())
