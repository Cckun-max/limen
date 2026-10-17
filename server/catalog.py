"""Shared public market catalog and a bounded set of lazily loaded chart feeds."""
import asyncio
import math

import httpx
from fastapi import HTTPException

from .market import INFO_URL, MarketService
from .models import now_ms
from .ibkr import Broker, IBMarket


def finite(value, *, positive=False):
    try:
        result = float(value)
        return result if math.isfinite(result) and (not positive or result > 0) else None
    except (ValueError, TypeError):
        return None


def parse_catalog(raw, at):
    metadata, contexts = raw
    universe = metadata["universe"]
    if len(universe) != len(contexts) or not universe:
        raise ValueError("币种目录与行情数量不一致")
    rows = []
    seen = set()
    for asset, ctx in zip(universe, contexts):
        symbol = asset["name"]
        if not isinstance(symbol, str) or not symbol or len(symbol) > 64 or symbol in seen:
            raise ValueError("无效或重复的币种标识")
        seen.add(symbol)
        mark, prev = finite(ctx.get("markPx"), positive=True), finite(ctx.get("prevDayPx"), positive=True)
        oi = finite(ctx.get("openInterest"))
        rows.append({"symbol": symbol, "instrument_id": f"hyperliquid:perp:{symbol}",
                     "mark": mark, "change_pct": (mark / prev - 1) * 100 if mark and prev else None,
                     "volume_usd": finite(ctx.get("dayNtlVlm")), "oi_base": oi,
                     "oi_usd": mark * oi if mark and oi is not None else None,
                     "funding": finite(ctx.get("funding")), "delisted": bool(asset.get("isDelisted")),
                     "max_leverage": asset.get("maxLeverage"), "sz_decimals": asset.get("szDecimals", 0),
                     "received_at": at})
    return rows


class MarketHub:
    MAX_FEEDS = 4

    def __init__(self, store, *, live=True):
        self.store, self.live = store, live
        self.catalog = store.get("market_catalog") or {"items": [], "received_at": None}
        self.error = None
        self.feeds = {"BTC": MarketService(store)}
        self.touched = {"BTC": now_ms()}
        self.clients = {}
        self.lock = asyncio.Lock()
        self.request_lock = asyncio.Lock()
        self.http = None
        self.task = None
        self.raw_catalog = None
        self.broker = Broker(store)

    async def start(self):
        if not self.live:
            return
        self.http = httpx.AsyncClient(timeout=12)
        self.task = asyncio.create_task(self._loop())
        self.feeds["BTC"].info_provider = self.info
        self.feeds["BTC"].metadata_time = lambda: self.catalog["received_at"]
        await self.feeds["BTC"].start()
        await self.broker.start()

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        for feed in self.feeds.values():
            await feed.stop()
        await self.broker.stop()
        if self.http:
            await self.http.aclose()

    async def info(self, body, *, fresh=False):
        if not fresh and body["type"] == "metaAndAssetCtxs" and self.raw_catalog and now_ms() - self.catalog["received_at"] < 30_000:
            return self.raw_catalog
        # One request at a time across all feeds. Large gap repairs (up to 5000
        # candles) are spaced too, rather than unlimited bursts.
        async with self.request_lock:
            response = await self.http.post(INFO_URL, json=body)
            response.raise_for_status()
            result = response.json()
            if body["type"] == "metaAndAssetCtxs":
                self.accept(result)
            weight = 20 if body["type"] == "metaAndAssetCtxs" else 4 + len(result) // 60
            await asyncio.sleep(max(0.2, weight / 12))
            return result

    def accept(self, raw):
        at = now_ms()
        rows = parse_catalog(raw, at)
        # Keep removed symbols visible in saved watchlists, but never selectable.
        names = {row["symbol"] for row in rows}
        rows += [{**row, "delisted": True} for row in self.catalog["items"] if row["symbol"] not in names]
        self.catalog = {"items": rows, "received_at": at}
        self.raw_catalog = raw
        self.error = None
        self.store.put("market_catalog", self.catalog)

    async def _loop(self):
        while True:
            try:
                await self.info({"type": "metaAndAssetCtxs"}, fresh=True)
                async with self.lock:
                    for symbol in list(self.feeds):
                        if not self.clients.get(symbol) and now_ms() - self.touched[symbol] > 120_000:
                            await self.feeds.pop(symbol).stop()
                            self.touched.pop(symbol, None)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.error = "行情目录更新失败，正在重试；显示上次缓存。"
            await asyncio.sleep(20)

    def public(self):
        at = self.catalog["received_at"]
        return {**self.catalog, "items": [*self.broker.rows(), *self.catalog["items"]],
                "stale": not at or now_ms() - at > 65_000,
                "error": self.error, "scope": "Hyperliquid 永续 / IBKR 美股与期货", "refresh_seconds": 20}

    def validate(self, symbol):
        if symbol in self.broker.instruments:
            return self.broker.instruments[symbol]
        row = next((r for r in self.catalog["items"] if r["symbol"] == symbol), None)
        if row and not row["delisted"]:
            return row
        if symbol == "BTC" and not self.catalog["items"]:
            return {"symbol": "BTC"}
        raise HTTPException(422, "该币种不在可用永续市场中，或已下架。")

    async def get(self, symbol):
        self.validate(symbol)
        async with self.lock:
            if symbol not in self.feeds:
                if len(self.feeds) >= self.MAX_FEEDS:
                    unused = [s for s in self.feeds if not self.clients.get(s)]
                    if not unused:
                        raise HTTPException(429, "最多同时打开 4 个币种，请先关闭其他行情页面。")
                    victim = min(unused, key=lambda s: self.touched[s])
                    await self.feeds.pop(victim).stop()
                    self.touched.pop(victim, None)
                if symbol in self.broker.instruments:
                    feed = IBMarket(self.store, self.broker, self.broker.instruments[symbol])
                else:
                    feed = MarketService(self.store, symbol)
                    feed.info_provider = self.info
                    feed.metadata_time = lambda: self.catalog["received_at"]
                self.feeds[symbol] = feed
                if self.live:
                    await feed.start()
            self.touched[symbol] = now_ms()
            return self.feeds[symbol]

    async def acquire(self, symbol):
        # No await between get returning and claiming the SSE lease.
        feed = await self.get(symbol)
        self.clients[symbol] = self.clients.get(symbol, 0) + 1
        return feed

    def release(self, symbol):
        self.clients[symbol] = max(0, self.clients.get(symbol, 1) - 1)
        self.touched[symbol] = now_ms()
