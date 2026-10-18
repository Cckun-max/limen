"""Read-only market adapter for a separately authenticated local TWS / IB Gateway."""
import asyncio
from contextlib import suppress
from datetime import datetime, timedelta, timezone
import math
import re
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from ib_async import IB, Contract, Future, Stock
from pydantic import BaseModel, ConfigDict, Field

from .ai_routes import local_write
from .ibkr_sessions import Sessions, normalize_bars, timestamp, weekly_bars
from .indicators import VERSION, calculate
from .models import TIMEFRAMES, now_ms

BAR_SIZES = {"1m":"1 min", "3m":"3 mins", "5m":"5 mins", "15m":"15 mins", "30m":"30 mins",
             "1h":"1 hour", "2h":"2 hours", "4h":"4 hours", "1d":"1 day"}
DURATIONS = {"1m":"3 D", "3m":"1 W", "5m":"1 W", "15m":"1 M", "30m":"2 M",
             "1h":"3 M", "2h":"6 M", "4h":"1 Y", "1d":"3 Y"}
FUTURES = {"ES": "CME", "NQ": "CME", "MES": "CME", "MNQ": "CME", "RTY": "CME", "M2K": "CME",
           "MGC": "COMEX"}
US_STOCK_EXCHANGES = {"NASDAQ", "ISLAND", "NYSE", "AMEX", "ARCA", "BATS", "IEX", "EDGEA", "NYSENAT"}


def broker_message(message):
    # Some TWS locales send literal Unicode escapes instead of decoded text.
    return re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m[1], 16)), str(message)).strip()


def history_no_data(error):
    message = broker_message(getattr(error, "message", "")).casefold()
    return getattr(error, "code", None) == 162 and any(part in message for part in (
        "hmds query returned no data", "historical market data query returned no data",
        "历史市场数据查询未返回数据", "歷史市場數據查詢未返回數據"))


def request_error(error):
    code = getattr(error, "code", None)
    if code is not None:
        return f"IBKR {code}：{broker_message(getattr(error, 'message', str(error)))[:400]}"
    if isinstance(error, TimeoutError):
        return "请求超时，稍后自动重试"
    return str(error)[:180] if isinstance(error, ValueError) else "数据请求失败，稍后自动重试"


def number(value, *, positive=False):
    try:
        n = float(value)
        return n if math.isfinite(n) and (n > 0 if positive else n >= 0) else None
    except (TypeError, ValueError):
        return None


class ConnectionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    port: int = Field(default=7497, ge=1024, le=65535)
    client_id: int = Field(default=71, ge=1, le=999999)
    # A conservative historical cutoff is independent of quote entitlements.
    delayed: bool = True


class Broker:
    def __init__(self, store):
        self.store = store
        self.config = ConnectionSettings(**(store.get("ibkr:settings") or {}))
        self.instruments = store.get("ibkr:instruments") or {}
        self.candidates = {}
        self.discovery_contracts = {}
        self.last_symbol_search = 0.0
        self.ib = IB()
        self.ib.RaiseRequestErrors = True
        self.ib.errorEvent += self._on_error
        self.task = None
        self.lock = asyncio.Lock()
        self.feeds = {}
        self.generation = 0
        self.connecting = False
        self.upstream = False
        self.error = None
        self.last_check = 0

    @property
    def connected(self):
        return self.ib.isConnected() and self.upstream

    def _on_error(self, req_id, code, message, contract=None):
        if code in {1100, 2110}:
            self.upstream = False
            self.error = "盈透上游连接中断，等待恢复"
        elif code in {1101, 1102}:
            self.upstream = True
            self.generation += 1
            self.error = None
        elif code not in {2104, 2106, 2107, 2108, 2158}:
            detail = f"IBKR {code}：{broker_message(message)[:400]}"
            if req_id < 0:
                self.error = detail
            elif contract and code in {200, 354, 10089, 10090, 10167, 10168, 10186, 10189}:
                feed = self.feeds.get(f"ibkr:{contract.conId}")
                if feed:
                    feed.errors["quote"] = detail
            # Awaited history/calendar errors are handled by their requesting
            # feed; they must not become global connection errors for every symbol.

    def public(self):
        return {"connected": self.connected, "connecting": self.connecting,
                "read_only": True, "last_check": self.last_check or None,
                "history_delay_minutes": 15 if self.config.delayed else 0,
                "instruments": self.rows()}

    def rows(self):
        result = []
        for key, item in self.instruments.items():
            feed = self.feeds.get(key)
            quote = feed.quote if feed else self.store.get(f"quote:{key}")
            result.append({**item, "symbol": key, "instrument_id": key,
                           "mark": quote.get("mark") if quote else None,
                           "change_pct": quote.get("change_pct") if quote else None,
                           "volume_usd": None, "oi_usd": None, "funding": None,
                           "delisted": False, "sz_decimals": 0,
                           "received_at": quote.get("received_at") if quote else None,
                           "data_mode": quote.get("data_mode") if quote else "unavailable",
                           "stale": not feed or feed.overview()["quote_stale"]})
        return result

    async def start(self):
        if self.config.enabled and not self.task:
            self.task = asyncio.create_task(self._loop())

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        self.ib.disconnect()
        self.upstream = False
        self.connecting = False

    async def _loop(self):
        while True:
            try:
                if not self.ib.isConnected():
                    self.connecting = True
                    # Use the documented socket client handshake directly. IB.connectAsync
                    # also synchronizes positions even with fetchFields=0. We need no
                    # portfolio, account, execution or order requests at all.
                    self.ib.wrapper.clientId = self.config.client_id
                    await self.ib.client.connectAsync("127.0.0.1", self.config.port, self.config.client_id, timeout=5)
                    self.ib.reqMarketDataType(3 if self.config.delayed else 1)
                    self.upstream = True
                    self.generation += 1
                    self.error = None
                self.connecting = False
                await asyncio.wait_for(self.ib.reqCurrentTimeAsync(), 5)
                self.last_check = now_ms()
                await asyncio.sleep(20)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.connecting = False
                self.upstream = False
                self.ib.disconnect()
                self.error = "未连接。请登录 TWS / IB Gateway，启用只读 Socket API，并核对端口。"
                await asyncio.sleep(10)

    def require(self):
        if not self.connected:
            raise HTTPException(503, "行情暂不可用，请稍后重试")

    async def discover(self, query):
        """Search the broker's catalog without subscribing to market data."""
        self.require()
        query = query.strip().upper()
        if not re.fullmatch(r"[A-Z0-9 .&-]{1,40}", query):
            raise HTTPException(422, "请输入证券代码或英文名称，例如 NVDA、NV、NVIDIA")
        async with self.lock:
            # IBKR requires at least one second between symbol searches.
            await asyncio.sleep(max(0, 1.05 - (time.monotonic() - self.last_symbol_search)))
            self.last_symbol_search = time.monotonic()
            try:
                matches = await asyncio.wait_for(self.ib.reqMatchingSymbolsAsync(query), 12)
                if matches is None:
                    raise TimeoutError()
            except Exception:
                raise HTTPException(502, "品种查询暂未完成，请稍后重试")
        result = {}
        for match in matches:
            c = match.contract
            if c.secType != "STK" or c.currency != "USD" or c.primaryExchange not in US_STOCK_EXCHANGES or c.conId <= 0:
                continue
            # Qualify only the selected result; never treat a search hit as a complete contract.
            self.discovery_contracts[c.conId] = Stock(c.symbol, "SMART", "USD", conId=c.conId, primaryExchange=c.primaryExchange)
            result[c.conId] = {"con_id": c.conId, "symbol": f"ibkr:{c.conId}",
                "display_symbol": c.symbol, "name": c.description or c.symbol,
                "exchange": c.primaryExchange, "currency": c.currency, "asset_type": "stock", "expiry": ""}
        while len(self.discovery_contracts) > 200:
            del self.discovery_contracts[next(iter(self.discovery_contracts))]
        return sorted(result.values(), key=lambda r: (r["display_symbol"] != query,
            not r["display_symbol"].startswith(query), r["display_symbol"], r["con_id"]))[:20]

    async def resolve_and_add(self, con_id):
        if con_id not in self.candidates:
            contract = self.discovery_contracts.get(con_id)
            if contract is None:
                raise HTTPException(422, "请先搜索并选择盈透返回的具体品种")
            self.require()
            async with self.lock:
                try:
                    details = await asyncio.wait_for(self.ib.reqContractDetailsAsync(contract), 12)
                except Exception:
                    raise HTTPException(502, "合约确认失败，请稍后重试")
            valid = [d for d in details if d.contract.conId == con_id
                     and d.contract.primaryExchange in US_STOCK_EXCHANGES]
            self._remember_details(valid, "STK")
            if con_id not in self.candidates:
                raise HTTPException(502, "盈透未返回该品种的完整合约信息，请重新搜索")
        return self.add(con_id)

    async def search(self, query, kind):
        self.require()
        query = query.strip().upper()
        if not re.fullmatch(r"[A-Z0-9 .-]{1,20}", query):
            raise HTTPException(422, "请输入证券代码，例如 AAPL、SPY、ES")
        if kind == "FUT" and query not in FUTURES:
            raise HTTPException(422, "目前支持股指期货 ES / NQ / MES / MNQ / RTY / M2K，以及微型黄金 MGC")
        contract = Future(query, exchange=FUTURES[query], currency="USD") if kind == "FUT" else Stock(query, "SMART", "USD")
        async with self.lock:
            try:
                details = await asyncio.wait_for(self.ib.reqContractDetailsAsync(contract), 12)
            except Exception:
                raise HTTPException(502, "查询失败，请检查代码后重试")
            await asyncio.sleep(.6)
        return self._remember_details(details, kind)

    def _remember_details(self, details, kind):
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        result = []
        for detail in details:
            c = detail.contract
            expiry = detail.realExpirationDate or c.lastTradeDateOrContractMonth
            if c.secType != kind or c.currency != "USD" or (kind == "FUT" and expiry[:8] < today):
                continue
            key = f"ibkr:{c.conId}"
            item = {"symbol": key, "instrument_id": key, "con_id": c.conId, "provider": "ibkr",
                    "display_symbol": c.localSymbol or c.symbol, "native_symbol": c.symbol,
                    "name": detail.longName, "asset_type": "future" if kind == "FUT" else "stock",
                    "exchange": c.exchange, "primary_exchange": c.primaryExchange, "currency": c.currency,
                    "expiry": expiry, "multiplier": c.multiplier, "tick_size": detail.minTick,
                    "timezone": detail.timeZoneId, "session": "全部交易时段" if kind == "FUT" else "常规交易时段",
                    "volume_unit": "手" if kind == "FUT" else "股",
                    "contract": {k: getattr(c,k) for k in ("conId","symbol","secType","exchange","primaryExchange","currency","localSymbol","lastTradeDateOrContractMonth","multiplier","tradingClass")}}
            self.candidates[c.conId] = item
            result.append(item)
        return sorted(result, key=lambda x: (x["expiry"], x["display_symbol"]))[:30]

    def add(self, con_id):
        item = self.candidates.get(con_id)
        if item is None:
            raise HTTPException(422, "请先查询并选择盈透返回的具体合约")
        if len(self.instruments) >= 50 and item["symbol"] not in self.instruments:
            raise HTTPException(422, "最多保存 50 个盈透品种")
        self.instruments[item["symbol"]] = item
        self.store.put("ibkr:instruments", self.instruments)
        return item


class IBMarket:
    def __init__(self, store, broker, item):
        self.store, self.broker, self.item = store, broker, item
        self.symbol = self.instrument_id = item["symbol"]
        self.contract = Contract.create(**item["contract"])
        self.quote = store.get(f"quote:{self.symbol}")
        self.sessions = None
        self.session_payload = store.get(f"sessions:{self.symbol}")
        if self.session_payload:
            self.sessions = Sessions(self.session_payload)
        self.revisions = {tf: 0 for tf in TIMEFRAMES}
        self.last_rest = {tf: 0 for tf in TIMEFRAMES}
        self.last_full = {tf: 0 for tf in TIMEFRAMES}
        self.fetch_started = {tf: 0 for tf in TIMEFRAMES}
        self.errors = {}
        self.feature_cache = {}
        self.generation = -1
        self.ticker = None
        self.syncing = False
        self.sync_event = asyncio.Event()
        self.task = None

    async def start(self):
        self.broker.feeds[self.symbol] = self
        self.task = asyncio.create_task(self._loop())

    async def reset(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self._unsubscribe()
        self.quote = None
        self.last_rest = {tf: 0 for tf in TIMEFRAMES}
        self.feature_cache.clear()
        self.generation = -1
        self.task = asyncio.create_task(self._loop())

    def _unsubscribe(self):
        if self.ticker:
            self.ticker.updateEvent -= self._quote
            if self.broker.ib.isConnected():
                with suppress(Exception):
                    self.broker.ib.cancelMktData(self.contract)
            self.ticker = None

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self._unsubscribe()
        if self.quote:
            self.store.put(f"quote:{self.symbol}", self.quote)
        self.broker.feeds.pop(self.symbol, None)

    def _quote(self, ticker):
        last, prev = number(ticker.last, positive=True), number(ticker.close, positive=True)
        source_time = ticker.delayedLastTimestamp if ticker.marketDataType in {3,4} else ticker.lastTimestamp
        self.quote = {"instrument_id": self.symbol, "symbol": self.symbol, "mark": last, "mid": None,
                      "price_kind": "last", "previous_day": prev,
                      "change_pct": (last/prev-1)*100 if last and prev else None,
                      "volume_usd": None, "oi_base": None, "funding": None,
                      "volume": number(ticker.volume), "bid": number(ticker.bid, positive=True),
                      "ask": number(ticker.ask, positive=True), "bid_size": number(ticker.bidSize), "ask_size": number(ticker.askSize),
                      "received_at": now_ms(), "source_time": timestamp(source_time) if source_time else None,
                      "source": "IBKR", "data_mode": {1:"live",2:"frozen",3:"delayed",4:"delayed_frozen"}.get(ticker.marketDataType,"unknown")}
        if any(self.quote[key] is not None for key in ("mark", "bid", "ask")):
            self.errors.pop("quote", None)

    def cutoff(self):
        return now_ms() - (900_000 if self.broker.config.delayed else 0)

    def features(self, timeframe):
        cutoff = self.cutoff() // 1000 * 1000
        key = (self.revisions[timeframe], cutoff, self.last_rest[timeframe], self.generation)
        cached = self.feature_cache.get(timeframe)
        if cached and cached[0] == key:
            return cached[1:]
        # A live -> delayed switch must not expose newer, already-final cached
        # values as a supposedly delayed forming candle. Old forming cache is
        # hidden until this connection has reconciled the period again.
        bars = [b for b in self.store.bars(timeframe, symbol=self.symbol)
                if b.time <= cutoff and (b.end <= cutoff - 2000 if b.status == "confirmed" else
                    self.last_rest[timeframe] > 0 and self.generation == self.broker.generation
                    and b.received_at >= self.fetch_started[timeframe])]
        values = calculate(bars, self.sessions.adjacent if self.sessions else None)
        self.feature_cache[timeframe] = (key, bars, values)
        return bars, values

    def _quality(self, tf, bars):
        gaps = self.sessions.gaps(bars, tf, self.cutoff()) if self.sessions else []
        fresh = (self.broker.connected and self.generation == self.broker.generation
                 and now_ms() - self.last_rest[tf] < 180_000)
        status = "warming" if not bars else "stale" if not fresh else "gap" if gaps else "delayed" if self.broker.config.delayed else "fresh"
        if fresh and not gaps and self.sessions and not self.sessions.at(now_ms()):
            status = "closed"
        confirmed = next((b for b in reversed(bars) if b.status == "confirmed"), None)
        return {"status": status, "gaps": gaps, "pending": 0, "count": len(bars),
                "last_confirmed_end": confirmed.end if confirmed else None,
                "last_rest": self.last_rest[tf] or None, "error": self.errors.get(tf) or self.errors.get("calendar"),
                "data_cutoff": self.cutoff(), "calendar_source": "IBKR SCHEDULE",
                "history_delay_minutes": 15 if self.broker.config.delayed else 0}

    def chart(self, tf):
        bars, values = self.features(tf)
        return {"instrument_id": self.symbol, "symbol": self.symbol, "instrument": self.item,
                "timeframe": tf, "bars": [b.public() for b in bars[-800:]], "indicators": values[-800:],
                "revision": self.revisions[tf], "algorithm_version": VERSION + ":ibkr-sessions-v1",
                "quality": self._quality(tf,bars), "as_of": now_ms(), "coverage_start": bars[0].time if bars else None}

    def overview(self):
        summaries = {}
        for tf in TIMEFRAMES:
            bars, values = self.features(tf)
            summaries[tf] = {"revision":self.revisions[tf], "quality":self._quality(tf,bars),
                             "confirmed_indicators":next((v for v in reversed(values) if not v["provisional"]),None),
                             "last_close":next((b.close for b in reversed(bars) if b.status == "confirmed"),None)}
        quote_stale = (not self.broker.connected or self.generation != self.broker.generation
                       or not self.quote or now_ms()-self.quote["received_at"] > 90_000)
        return {"instrument_id":self.symbol,"symbol":self.symbol,"instrument":self.item,"quote":self.quote,
                "quote_stale":bool(quote_stale),"transport":"polling" if self.broker.connected else "offline",
                "ws_connected":False,"last_ws":None,"syncing":self.syncing,"reconnects":0,
                "timeframes":summaries,"errors":{**self.errors, **({"connection":"行情暂不可用，请稍后重试"} if self.broker.error else {})},
                "server_time":now_ms(),"algorithm_version":VERSION+":ibkr-sessions-v1",
                "data_cutoff":self.cutoff(),"history_delay_minutes":15 if self.broker.config.delayed else 0}

    async def _schedule(self):
        now = now_ms()
        if self.session_payload and now-self.session_payload.get("fetched_at",0) < 3600_000 and self.session_payload["end"] >= self.cutoff():
            return
        existing = self.session_payload
        rows = {(r["date"],r["start"]):r for r in existing["rows"]} if existing else {}
        oldest = existing["start"] if existing else now
        end = datetime.now(timezone.utc)
        # First load covers all requested daily history, then refresh only recent dates.
        for i in range(1 if existing else 4):
            try:
                async with self.broker.lock:
                    try:
                        result = await asyncio.wait_for(self.broker.ib.reqHistoricalScheduleAsync(
                            self.contract, 30 if existing else 365, end, useRTH=self.item["asset_type"]=="stock"), 20)
                    finally:
                        await asyncio.sleep(.6)
            except Exception as error:
                if i > 0 and rows and not existing and history_no_data(error):
                    break  # No older history; keep the recent verified schedule.
                raise  # Permissions, timeouts and first-page failures remain errors.
            if not result.sessions:
                if rows and not existing:
                    break  # A dated future may not have existed this far back.
                raise ValueError("IBKR 未返回交易日历")
            zone = result.timeZone
            response_start, response_end = timestamp(result.startDateTime,zone), timestamp(result.endDateTime,zone)
            rows = {key:r for key,r in rows.items() if r["end"] <= response_start or r["start"] >= response_end}
            for s in result.sessions:
                start = timestamp(s.startDateTime,zone)
                rows[(s.refDate,start)] = {"date":s.refDate,"start":start,"end":timestamp(s.endDateTime,zone)}
            oldest = min(oldest, timestamp(result.startDateTime,zone))
            end -= timedelta(days=365)
        self.session_payload = {"rows":list(rows.values()),"zone":zone,"start":oldest,
                                "end":max(now,max(r["end"] for r in rows.values())),"fetched_at":now}
        self.sessions = Sessions(self.session_payload)
        self.store.put(f"sessions:{self.symbol}",self.session_payload)
        self.feature_cache.clear()

    async def reconcile(self):
        self.syncing = True
        try:
            try:
                await self._schedule()
            except Exception as error:
                self.errors["calendar"] = f"交易日历：{request_error(error)}"
                return
            self.errors.pop("calendar",None)
            for tf in ("15m","1h","4h","1d","5m","30m","2h","1m","3m"):
                cutoff = self.cutoff()
                try:
                    cached = self.store.bars(tf,symbol=self.symbol)
                    full = (not cached or now_ms()-self.last_full[tf] >= 6*3600_000
                            or bool(self.sessions.gaps(cached,tf,cutoff)))
                    # Reload full coverage periodically; ordinary polls only overlap
                    # the tail, including the prior session and settlement corrections.
                    tail_days = max(2, math.ceil((cutoff-cached[-1].time)/86400_000)+2) if cached else 2
                    duration = DURATIONS[tf] if full else "1 M" if tf == "1d" else f"{tail_days} D"
                    async with self.broker.lock:
                        try:
                            started = now_ms()
                            raw = await self.broker.ib.reqHistoricalDataAsync(self.contract,
                                endDateTime=datetime.fromtimestamp(cutoff/1000,timezone.utc),
                                durationStr=duration,barSizeSetting=BAR_SIZES[tf],whatToShow="TRADES",
                                useRTH=self.item["asset_type"]=="stock",formatDate=2,keepUpToDate=False,timeout=20)
                        finally:
                            await asyncio.sleep(.6)
                    if not raw:
                        raise ValueError("无历史 K 线：请核对行情权限和合约历史范围")
                    bars = normalize_bars(raw,tf,self.symbol,self.sessions,cutoff,now_ms())
                    if self.store.merge(bars):
                        self.revisions[tf] += 1
                    self.last_rest[tf] = now_ms()
                    self.fetch_started[tf] = started
                    self.feature_cache.pop(tf,None)
                    if full:
                        self.last_full[tf] = now_ms()
                    self.errors.pop(tf,None)
                    if tf == "1d":
                        weekly = weekly_bars(self.features("1d")[0],self.sessions,cutoff)
                        if self.store.merge(weekly):
                            self.revisions["1w"] += 1
                        self.last_rest["1w"] = now_ms()
                        self.fetch_started["1w"] = started
                        self.feature_cache.pop("1w",None)
                        self.errors.pop("1w",None)
                except Exception as error:
                    self.errors[tf] = request_error(error)
                    self.last_rest[tf] = 0
                    if tf == "1d":
                        self.last_rest["1w"] = 0
                        self.errors["1w"] = self.errors[tf]
        finally:
            self.syncing = False

    async def _loop(self):
        while True:
            self.sync_event.clear()
            if self.broker.connected:
                try:
                    if self.generation != self.broker.generation:
                        self._unsubscribe()
                        self.quote = None
                        self.last_rest = {tf:0 for tf in TIMEFRAMES}
                        self.ticker = self.broker.ib.reqMktData(self.contract,"",False,False)
                        self.ticker.updateEvent += self._quote
                        self.generation = self.broker.generation
                    await self.reconcile()
                    self.errors.pop("sync",None)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self.errors["sync"] = f"行情同步：{request_error(error)}"
            try:
                await asyncio.wait_for(self.sync_event.wait(),60 if self.broker.connected else 3)
                await asyncio.sleep(15)
            except asyncio.TimeoutError:
                pass


router = APIRouter(prefix="/api/v1/ibkr")


@router.get("/status")
async def status(request: Request):
    return request.app.state.hub.broker.public()


@router.get("/search")
async def search(request:Request,q:str,kind:str="STK"):
    if kind not in {"STK","FUT"}:
        raise HTTPException(422,"仅支持美股 / ETF 与期货")
    return await request.app.state.hub.broker.search(q,kind)


@router.get("/discover")
async def discover(request:Request,q:str):
    return await request.app.state.hub.broker.discover(q)


class AddInstrument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    con_id:int = Field(gt=0)


@router.post("/instruments",dependencies=[Depends(local_write)])
async def add(body:AddInstrument,request:Request):
    return await request.app.state.hub.broker.resolve_and_add(body.con_id)
