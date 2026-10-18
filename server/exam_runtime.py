"""A persistent paper ledger driven only by the IBKR candle data clock.

Signals use newly confirmed candles. A later, newly received candle snapshot's
sampled close executes them. We deliberately do not backdate fills to a next-bar
open that had already happened when the signal became observable. Candle prices
are sampling simulations, not executable market quotes.
"""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
import math
import logging
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from .exams import AccountCreate, ExamStore, STRATEGIES, valid_instrument
from .indicators import ema
from .models import Candle, TIMEFRAMES, now_ms


ACTIVE = {"running"}
TERMINAL = {"passed", "failed", "terminated"}
logger = logging.getLogger(__name__)


def money(value):
    return round(value, 8)


class ExamService:
    def __init__(self, store, hub):
        self.store = ExamStore(store)
        self.hub = hub
        self.task = None
        self.feeds = {}
        self.tick_lock = asyncio.Lock()
        # Ledger positions survive restarts. Never replay cached missed signals.
        with store.lock, store.db:
            for account in self.store.accounts():
                if account["status"] == "running" or account.get("position"):
                    account["_state"]["baseline_needed"] = True
                    account["_state"]["not_before"] = now_ms()
                    account["_state"]["pending"] = None
                    account["run_state"] = "recovering"
                    account["last_error"] = "运行曾中断；保留持仓，重新取得新鲜数据后恢复，不补做错失交易。"
                    self.store.event(account, "interrupted", account["last_error"], now_ms())
                    self.store.save(account)

    def instruments(self):
        return [deepcopy(item) for item in self.hub.broker.rows() if valid_instrument(item)]

    def create_account(self, body: AccountCreate, *, at=None, parent_id=None, snapshots=None):
        at = at or now_ms()
        preset = deepcopy(snapshots["preset"]) if snapshots else self.store.preset(body.preset_id)
        strategy = deepcopy(snapshots["strategy"]) if snapshots else next((item for item in STRATEGIES if item["id"] == body.strategy_id), None)
        if strategy is None:
            raise HTTPException(422, "请选择可执行的基础策略")
        instrument = self.hub.broker.instruments.get(body.symbol)
        if not instrument or not valid_instrument(instrument, at):
            raise HTTPException(422, "请选择已添加、未到期且具备合约乘数及最小跳动的 USD 具体期货合约")
        if body.quantity > preset["max_contracts"]:
            raise HTTPException(422, "交易手数超过套餐持仓上限")
        instrument = deepcopy(snapshots["instrument"] if snapshots else instrument)
        instrument["multiplier"] = float(instrument["multiplier"])
        instrument["tick_size"] = float(instrument["tick_size"])
        initial = preset["initial_balance"]
        account = {"id": str(uuid4()), "name": body.name, "status": "ready", "symbol": body.symbol,
                   "timeframe": body.timeframe, "quantity": body.quantity, "strategy": deepcopy(strategy),
                   "preset": preset, "instrument": instrument, "position": None,
                   "balance": initial, "equity": initial, "net_pnl": 0, "unrealized_pnl": 0,
                   "drawdown_limit": initial - preset["max_drawdown"], "daily_pnl": 0,
                   "trading_days": 0, "trade_count": 0, "total_fees": 0, "total_slippage": 0,
                   "best_day_pnl": 0, "consistency_ratio": None, "daily_results": [],
                   "run_state": "ready", "last_error": None, "created_at": at, "updated_at": at,
                   "started_at": None, "ended_at": None, "data_as_of": None, "data_mode": None,
                   "parent_account_id": parent_id,
                   "execution_model": "confirmed_signal_then_observed_close",
                   "execution_note": "已确认柱给出信号，之后首次新鲜 K 线采样收盘价成交；计手续费和不利滑点，不回填下一柱开盘价。浮亏按可确认的成交后区间检查。",
                   "_state": {"baseline_needed": True, "last_data_clock": 0, "last_received_at": 0,
                              "last_signal_end": 0, "anchor": 0, "last_bar_time": 0, "pending": None,
                              "requested_status": None, "day": None, "day_start_balance": initial,
                              "days": {}, "eod_high": initial, "eod_finalized": None, "halt_day": None,
                              "quality_blocked": False, "last_mark": None, "last_risk_bar_end": 0, "not_before": at,
                              "force_flat": False}}
        with self.store.store.lock, self.store.store.db:
            self.store.save(account)
            self.store.event(account, "created", "模拟考试账号已创建；规则、策略版本及合约乘数已冻结。", at)
            self.store.equity(account, at)
        return self.store.public(account)

    def action(self, key, action):
        with self.store.store.lock, self.store.store.db:
            account = self.store.get(key)
            state = account["_state"]
            at = now_ms()
            if action == "restart":
                # A repeat attempt preserves exactly the original frozen rules.
                body = AccountCreate(name=(account["name"][:72] + " · 重考"), preset_id=account["preset"]["id"],
                                     strategy_id=account["strategy"]["id"], symbol=account["symbol"],
                                     timeframe=account["timeframe"], quantity=account["quantity"])
                return self.create_account(body, at=at, parent_id=key, snapshots=account)
            if action in {"start", "resume"}:
                expected = "ready" if action == "start" else "paused"
                if account["status"] != expected:
                    raise HTTPException(409, "当前账号状态不能执行此操作")
                account["status"] = "running"
                account["started_at"] = account["started_at"] or at
                account["run_state"] = "waiting_for_data"
                account["last_error"] = None
                state["baseline_needed"] = True
                state["not_before"] = at
                state["pending"] = None
                state["requested_status"] = None
            elif action in {"pause", "terminate"}:
                if account["status"] in TERMINAL:
                    raise HTTPException(409, "已结束账号不能执行此操作，可创建重考账号")
                target = "paused" if action == "pause" else "terminated"
                state["pending"] = None
                if account["position"]:
                    account["status"] = "running"
                    state["requested_status"] = target
                    account["run_state"] = "closing"
                    account["last_error"] = "等待下一份新鲜行情平仓后" + ("暂停。" if target == "paused" else "结束。")
                else:
                    account["status"] = target
                    account["run_state"] = target
                    if target == "terminated":
                        account["ended_at"] = at
            else:
                raise HTTPException(422, "未知账号操作")
            account["updated_at"] = at
            self.store.event(account, action, {"start": "开始考试", "resume": "恢复考试；不补做暂停期间交易", "pause": "请求暂停", "terminate": "请求结束"}[action], at)
            self.store.save(account)
            return self.store.public(account)

    async def start(self):
        if not self.task and getattr(self.hub, "live", True):
            self.task = asyncio.create_task(self._loop())

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        for symbol in list(self.feeds):
            self.hub.release(symbol)
        self.feeds.clear()

    async def _loop(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Individual failures are handled below; a storage failure must
                # not silently discard a running task or a paper position.
                logger.exception("Paper exam background tick failed; retrying without discarding ledger")
            await asyncio.sleep(2)

    def _waiting(self, key, message, *, run_state="waiting_for_data", close_due=False):
        with self.store.store.lock, self.store.store.db:
            account = self.store.get(key)
            if account["status"] != "running":
                return
            if account["last_error"] != message:
                self.store.event(account, "data_unavailable", message, now_ms())
            account["last_error"] = message
            account["run_state"] = "closing" if account["_state"]["requested_status"] else run_state
            account["_state"]["baseline_needed"] = True
            account["_state"]["pending"] = None
            if close_due and account["position"]:
                if not account["_state"].get("force_flat"):
                    self.store.event(account, "missed_session_close", "收盘时缺少可用新价；保留持仓，等待下一份新鲜样本强平。", now_ms())
                account["_state"]["force_flat"] = True
                account["run_state"] = "closing"
            account["updated_at"] = now_ms()
            self.store.save(account)

    async def tick(self, *, at=None):
        async with self.tick_lock:
            at = at or now_ms()
            active = [a for a in self.store.accounts() if a["status"] == "running"]
            symbols = {a["symbol"] for a in active}
            for symbol in set(self.feeds) - symbols:
                self.hub.release(symbol)
                self.feeds.pop(symbol, None)
            for symbol in symbols - set(self.feeds):
                try:
                    self.feeds[symbol] = await self.hub.acquire(symbol)
                except Exception as error:
                    text = error.detail if isinstance(error, HTTPException) else "行情连接暂不可用"
                    for account in active:
                        if account["symbol"] == symbol:
                            self._waiting(account["id"], f"{text}；后台会重试，考试记录已保留。", run_state="waiting_for_feed")
            snapshots = {}
            for account in active:
                feed = self.feeds.get(account["symbol"])
                if not feed:
                    continue
                try:
                    group = (account["symbol"], account["timeframe"])
                    if group not in snapshots:
                        snapshots[group] = feed.chart(account["timeframe"])
                    chart = snapshots[group]
                    quality = chart.get("quality", {})
                    self.step(account["id"], chart.get("bars", []), at=at, quality=quality,
                              data_cutoff=quality.get("data_cutoff"), sessions=getattr(feed, "sessions", None))
                except Exception as error:
                    text = error.detail if isinstance(error, HTTPException) else "考试行情处理失败，等待有效数据"
                    self._waiting(account["id"], str(text)[:250])

    @staticmethod
    def _day(at, preset, sessions=None):
        if sessions:
            session = sessions.at(at)
            if session:
                return session["date"]
        local = datetime.fromtimestamp(at / 1000, ZoneInfo(preset["timezone"]))
        if local.strftime("%H:%M") >= preset["session_reopen"]:
            local += timedelta(days=1)
        return local.strftime("%Y%m%d")

    @staticmethod
    def _closed(at, preset, sessions=None):
        if sessions is not None and not sessions.at(at):
            return True
        local = datetime.fromtimestamp(at / 1000, ZoneInfo(preset["timezone"]))
        clock, weekday = local.strftime("%H:%M"), local.weekday()
        if weekday == 5 or weekday == 6 and clock < preset["session_reopen"] or weekday == 4 and clock >= preset["session_close"]:
            return True
        return preset["session_close"] <= clock < preset["session_reopen"]

    def _mark(self, account, price):
        pos = account["position"]
        unrealized = 0 if not pos else (price - pos["entry_price"]) * (1 if pos["side"] == "long" else -1) * pos["quantity"] * account["instrument"]["multiplier"]
        account["unrealized_pnl"] = money(unrealized)
        account["equity"] = money(account["balance"] + unrealized)
        account["net_pnl"] = money(account["equity"] - account["preset"]["initial_balance"])
        account["daily_pnl"] = money(account["equity"] - account["_state"]["day_start_balance"])
        account["_state"]["last_mark"] = price

    def _stats(self, account):
        days = account["_state"]["days"]
        account["daily_results"] = [{"date": key, **value} for key, value in sorted(days.items())]
        account["trading_days"] = sum(bool(day["opened"]) for day in days.values())
        best = max([0, *[day["net_pnl"] for day in days.values()]])
        profit = account["balance"] - account["preset"]["initial_balance"]
        account["best_day_pnl"] = money(best)
        account["consistency_ratio"] = best / profit if profit > 0 else None

    def _realized(self, account, value):
        day = account["_state"]["day"]
        account["_state"]["days"].setdefault(day, {"net_pnl": 0, "opened": False})
        account["_state"]["days"][day]["net_pnl"] = money(account["_state"]["days"][day]["net_pnl"] + value)
        account["balance"] = money(account["balance"] + value)

    def _open(self, account, direction, raw_price, data_at, observed_at, signal):
        preset, state = account["preset"], account["_state"]
        qty = account["quantity"]
        sign = 1 if direction == "long" else -1
        slip = account["instrument"]["tick_size"] * preset["slippage_ticks"]
        price = money(raw_price + sign * slip)
        if price <= 0:
            raise ValueError("Invalid simulated fill")
        fee = money(qty * preset["fee_per_contract"])
        slip_cost = money(slip * qty * account["instrument"]["multiplier"])
        key = str(uuid4())
        account["position"] = {"side": direction, "quantity": qty, "entry_price": price,
                               "entry_time": data_at, "entry_fee": fee, "trade_id": key}
        item = {"id": key, "status": "open", "side": direction, "quantity": qty,
                "entry_price": price, "entry_raw_price": raw_price, "entry_time": data_at,
                "entry_observed_at": observed_at, "entry_fee": fee, "entry_slippage": slip_cost,
                "exit_price": None, "exit_time": None, "exit_fee": 0, "exit_slippage": 0,
                "gross_pnl": None, "net_pnl": None, "exit_reason": None,
                "signal": deepcopy(signal), "price_source": "ibkr_candle_observed_close",
                "multiplier": account["instrument"]["multiplier"], "tick_size": account["instrument"]["tick_size"]}
        self._realized(account, -fee)
        state["days"][state["day"]]["opened"] = True
        account["trade_count"] += 1
        account["total_fees"] = money(account["total_fees"] + fee)
        account["total_slippage"] = money(account["total_slippage"] + slip_cost)
        self.store.trade(account, item)
        self.store.event(account, "entry", "基础策略模拟开仓", observed_at, side=direction, quantity=qty, price=price, data_time=data_at)
        self._mark(account, raw_price)
        self._stats(account)

    def _close(self, account, raw_price, data_at, observed_at, reason):
        pos = account["position"]
        if not pos:
            return
        sign = 1 if pos["side"] == "long" else -1
        slip = account["instrument"]["tick_size"] * account["preset"]["slippage_ticks"]
        # Price must remain a valid positive sample even for pathological custom costs.
        price = max(account["instrument"]["tick_size"], money(raw_price - sign * slip))
        fee = money(pos["quantity"] * account["preset"]["fee_per_contract"])
        gross = money((price - pos["entry_price"]) * sign * pos["quantity"] * account["instrument"]["multiplier"])
        slip_cost = money(abs(raw_price - price) * pos["quantity"] * account["instrument"]["multiplier"])
        trade = self.store.get_trade(pos["trade_id"])
        trade.update(status="closed", exit_price=price, exit_raw_price=raw_price, exit_time=data_at,
                     exit_observed_at=observed_at, exit_fee=fee, exit_slippage=slip_cost,
                     gross_pnl=gross, net_pnl=money(gross - fee - pos["entry_fee"]), exit_reason=reason,
                     exit_price_source="ibkr_candle_adverse_extreme" if reason in {"maximum_drawdown", "daily_loss_limit"} else "ibkr_candle_observed_close")
        self.store.trade(account, trade)
        self._realized(account, gross - fee)
        account["position"] = None
        account["total_fees"] = money(account["total_fees"] + fee)
        account["total_slippage"] = money(account["total_slippage"] + slip_cost)
        self._mark(account, raw_price)
        self._stats(account)
        self.store.event(account, "exit", "模拟平仓：" + reason, observed_at, price=price, net_pnl=trade["net_pnl"], data_time=data_at)

    def _end(self, account, status, at, message):
        account["status"] = status
        account["run_state"] = status
        account["ended_at"] = at
        account["last_error"] = None
        account["_state"]["pending"] = None
        self.store.event(account, status, message, at)

    def _eod(self, account, at):
        state, preset = account["_state"], account["preset"]
        if state["eod_finalized"] == state["day"]:
            return
        old_limit = account["drawdown_limit"]
        state["eod_high"] = max(state["eod_high"], account["balance"])
        if preset["drawdown_mode"] == "eod_trailing":
            account["drawdown_limit"] = money(min(preset["initial_balance"], state["eod_high"] - preset["max_drawdown"]))
        state["eod_finalized"] = state["day"]
        self.store.event(account, "day_settled", "交易日结算；追踪亏损线仅按日终余额上移，最高到初始余额。", at,
                         trading_day=state["day"], balance=account["balance"], drawdown_limit=account["drawdown_limit"], previous_limit=old_limit)

    def _passing(self, account):
        preset = account["preset"]
        return (not account["position"] and account["balance"] - preset["initial_balance"] >= preset["profit_target"]
                and account["trading_days"] >= preset["min_trading_days"]
                and (preset["consistency_pct"] is None or account["consistency_ratio"] is not None
                     and account["consistency_ratio"] <= preset["consistency_pct"] + 1e-10))

    def _flat_risk(self, account, at):
        """Closing costs are debited before any possible reverse entry."""
        if account["equity"] <= account["drawdown_limit"]:
            self._end(account, "failed", at, "平仓成本后触及最大亏损线，考试失败。")
            return True
        limit, state = account["preset"]["daily_loss_limit"], account["_state"]
        if limit is not None and account["daily_pnl"] <= -limit:
            if state["halt_day"] != state["day"]:
                self.store.event(account, "daily_loss_limit", "平仓成本后达到日亏损上限，当日停止开仓。", at)
            state["halt_day"] = state["day"]
            state["pending"] = None
            return True
        return False

    def _risk(self, account, bars, latest, data_at, observed_at, *, new_interval):
        """Adverse whole-bar extremes are usable only if wholly after entry.

        A position opened partway through a forming bar cannot be stopped by an
        extreme that may predate its entry. Sampling close is used for that bar.
        Whole intervals observed after an outage are conservative risk evidence,
        but never generate reconstructed strategy fills.
        """
        pos = account["position"]
        if not pos:
            return False
        adverse = latest.close
        if new_interval:
            eligible = [bar for bar in bars if bar.time >= pos["entry_time"] and
                        ((bar.status == "confirmed" and bar.end <= data_at and bar.end > account["_state"].get("last_risk_bar_end", 0))
                         or bar is latest and bar.status == "forming")]
            if eligible:
                candidates = [bar.low if pos["side"] == "long" else bar.high for bar in eligible]
                adverse = min([adverse, *candidates]) if pos["side"] == "long" else max([adverse, *candidates])
        self._mark(account, adverse)
        if account["equity"] <= account["drawdown_limit"]:
            self._close(account, adverse, data_at, observed_at, "maximum_drawdown")
            self._end(account, "failed", observed_at, "持仓浮亏或账户余额触及最大亏损线，考试失败。")
            return True
        limit = account["preset"]["daily_loss_limit"]
        if limit is not None and account["daily_pnl"] <= -limit:
            self._close(account, adverse, data_at, observed_at, "daily_loss_limit")
            account["_state"]["halt_day"] = account["_state"]["day"]
            account["_state"]["pending"] = None
            self.store.event(account, "daily_loss_limit", "触及日亏损上限，已平仓，当交易日停止开仓。", observed_at)
            if account["equity"] <= account["drawdown_limit"]:
                self._end(account, "failed", observed_at, "平仓成本后触及最大亏损线，考试失败。")
            return True
        self._mark(account, latest.close)
        return False

    @staticmethod
    def _signal(account, confirmed, sessions=None):
        if not confirmed:
            return None
        # A data gap resets all indicator warmup rather than inventing continuity.
        segment = []
        for bar in confirmed:
            if segment and not (sessions.adjacent(segment[-1], bar) if sessions else segment[-1].end == bar.time):
                segment = []
            segment.append(bar)
        strategy = account["strategy"]
        if len(segment) < strategy["warmup_bars"]:
            return None
        if strategy["id"] == "ema-cross":
            closes = [bar.close for bar in segment]
            fast, slow = ema(closes, strategy["parameters"]["fast_period"]), ema(closes, strategy["parameters"]["slow_period"])
            if fast[-2] <= slow[-2] and fast[-1] > slow[-1]:
                return "long"
            if fast[-2] >= slow[-2] and fast[-1] < slow[-1]:
                return "short"
        elif strategy["id"] == "channel-breakout":
            prior = segment[-strategy["parameters"]["lookback"] - 1:-1]
            if segment[-1].close > max(bar.high for bar in prior):
                return "long"
            if segment[-1].close < min(bar.low for bar in prior):
                return "short"
        return None

    def step(self, key, incoming, *, at=None, quality=None, data_cutoff=None, sessions=None):
        """Apply one deterministic observation; repeats and corrections cannot refill.

        ``at`` is wall receipt time; ``data_cutoff`` is the market adapter's data
        clock (possibly 15 minutes delayed). Tests can supply both explicitly.
        No market prices are synthesized when no fresh observation exists.
        """
        at = at or now_ms()
        quality = quality or {"status": "fresh"}
        data_cutoff = data_cutoff if data_cutoff is not None else quality.get("data_cutoff", at)
        bars = [bar if isinstance(bar, Candle) else Candle(**bar) for bar in incoming]
        if not math.isfinite(data_cutoff) or data_cutoff > at + 5000:
            raise ValueError("Invalid market data cutoff")
        bars = sorted([bar for bar in bars if bar.time <= data_cutoff and
                       (bar.status != "confirmed" or bar.end <= data_cutoff - 2000)], key=lambda bar: bar.time)
        with self.store.store.lock, self.store.store.db:
            account = self.store.get(key)
            if account["status"] != "running":
                return self.store.public(account)
            state, preset = account["_state"], account["preset"]
            if not bars or quality.get("status") in {"warming", "stale"}:
                self._waiting(key, "行情尚未就绪或已中断；保留持仓与账本，暂停产生新交易。", close_due=self._closed(data_cutoff, preset, sessions))
                return self.store.public(self.store.get(key))
            latest = bars[-1]
            if any(not math.isfinite(v) or not 0 < v <= 1_000_000_000 for bar in bars for v in (bar.open, bar.high, bar.low, bar.close)):
                raise ValueError("Invalid candle prices")
            if any(bar.symbol != account["symbol"] or bar.timeframe != account["timeframe"] or bar.low > min(bar.open, bar.close)
                   or bar.high < max(bar.open, bar.close) for bar in bars):
                raise ValueError("Unexpected candle instrument or OHLC")
            if latest.received_at > at + 5000 or at - latest.received_at > 180000:
                self._waiting(key, "没有新鲜 K 线快照；等待更新，不用旧价格成交。", close_due=self._closed(data_cutoff, preset, sessions))
                return self.store.public(self.store.get(key))
            confirmed = [bar for bar in bars if bar.status == "confirmed"]
            latest_end = confirmed[-1].end if confirmed else 0
            # A corrected old candle is not a new available trading price. Both
            # source time and receipt must advance for any simulated execution.
            clock = min(data_cutoff, latest.end)
            fresh = (clock > state["last_data_clock"] and latest.received_at > state["last_received_at"]
                     and latest.time >= state["last_bar_time"])
            if not fresh:
                # Confirmation can arrive after we already observed the forming
                # bar near its boundary. Risk has its own confirmation cursor;
                # a source-clock cursor must not suppress its final extremes.
                risk_end = state.get("last_risk_bar_end", 0)
                if (account["position"] and latest.received_at > state["last_received_at"]
                        and any(bar.end > risk_end for bar in confirmed)):
                    mark = state["last_mark"]
                    self._risk(account, bars, latest, max(clock, state["last_data_clock"]), at, new_interval=True)
                    if account["position"] and mark is not None:
                        self._mark(account, mark)
                    state["last_risk_bar_end"] = max([risk_end, *[bar.end for bar in confirmed]])
                    state["last_received_at"] = latest.received_at
                    account["updated_at"] = at
                    self.store.save(account)
                    self.store.equity(account, max(clock, state["last_data_clock"]))
                return self.store.public(account)
            # Latest bars cannot be a stale previous session tail disguised by a
            # new REST receipt. Only the bar containing the data clock is a new
            # sampled close. A just-confirmed final bar is valid within 10 sec.
            if clock > latest.end + 10000 or data_cutoff - latest.end > 10000:
                self._waiting(key, "当前数据截止点没有对应 K 线；等待新行情，不追补成交。", close_due=self._closed(data_cutoff, preset, sessions))
                return self.store.public(self.store.get(key))
            if not valid_instrument(account["instrument"], at):
                state["requested_status"] = "terminated"
                account["last_error"] = "合约已到期，等待有效行情平仓后结束；请为新合约创建账号。"
            day = self._day(clock, preset, sessions)
            if state["day"] is not None and state["day"] != day:
                if account["position"] and not state.get("force_flat"):
                    state["force_flat"] = True
                    self.store.event(account, "missed_session_close", "跨交易日仍有持仓，记录中断并按下一份新鲜样本强平。", at)
                self._eod(account, at)
                state["day_start_balance"] = account["balance"]
            state["day"] = day
            state["days"].setdefault(day, {"net_pnl": 0, "opened": False})
            account["data_as_of"] = clock
            account["data_mode"] = "delayed" if quality.get("history_delay_minutes", 0) else "candle_sample"
            account["last_error"] = None if state["requested_status"] is None else account["last_error"]
            closed = self._closed(clock, preset, sessions)
            gap = quality.get("status") == "gap" or bool(quality.get("gaps"))
            was_baseline = state["baseline_needed"]
            if was_baseline:
                state["anchor"] = max(data_cutoff, state.get("not_before", account["started_at"] or at))
                state["last_signal_end"] = latest_end
                state["pending"] = None
                state["baseline_needed"] = False
                self.store.event(account, "data_baseline", "行情基线已建立；既有历史仅作预热，不补做开考前或中断期间的策略交易。", at,
                                 data_time=clock, delayed=account["data_mode"] == "delayed")
            # Risk always precedes signal exits or profit checks; entry-bar
            # pre-entry extremes are excluded by _risk's eligible interval test.
            risk_triggered = self._risk(account, bars, latest, clock, at, new_interval=True)
            if account["status"] == "running" and state.get("force_flat"):
                self._close(account, latest.close, clock, at, "missed_session_close")
                state["force_flat"] = False
                state["pending"] = None
                risk_triggered = True
            if account["status"] == "running" and state["requested_status"]:
                self._close(account, latest.close, clock, at, "user_" + state["requested_status"])
                target = state["requested_status"]
                state["requested_status"] = None
                account["status"] = target
                account["run_state"] = target
                account["last_error"] = None
                if target == "terminated":
                    account["ended_at"] = at
                self.store.event(account, target, "已取得新鲜价格并平仓，账号" + ("已暂停。" if target == "paused" else "已结束。"), at)
            if account["status"] == "running" and closed:
                self._close(account, latest.close, clock, at, "session_close")
                state["pending"] = None
                self._eod(account, at)
            can_open = (account["status"] == "running" and not closed and not gap and not was_baseline
                        and data_cutoff > state.get("not_before", 0) and state["halt_day"] != day and not risk_triggered)
            new_confirmed = [bar for bar in confirmed if bar.end > state["last_signal_end"]]
            missed = not was_baseline and len(new_confirmed) > 1
            if missed:
                state["pending"] = None
                can_open = False
                self.store.event(account, "missed_bars", "多根 K 线同时补到；跳过错失信号，不追补交易。", at, bars=len(new_confirmed))
            pending = state["pending"]
            if can_open and pending and clock > pending["data_time"] and latest.received_at > pending["observed_at"] and clock >= pending["signal_end"]:
                state["pending"] = None
                pos = account["position"]
                if pos and pos["side"] != pending["side"]:
                    self._close(account, latest.close, clock, at, "strategy_reverse")
                if self._flat_risk(account, at):
                    can_open = False
                elif self._passing(account):
                    self._end(account, "passed", at, "盈利目标、交易天数和盈利一致性均满足，平仓后通过考试。")
                elif not account["position"]:
                    self._open(account, pending["side"], latest.close, clock, at, pending)
                    # Entry costs and immediate unrealized slippage can breach
                    # a boundary even without an adverse price movement.
                    self._risk(account, [], latest, clock, at, new_interval=False)
            # A missing interval invalidates queued orders and suppresses signal
            # generation. Warmup is recomputed from contiguous real candles.
            if gap:
                state["pending"] = None
                if not state["quality_blocked"]:
                    self.store.event(account, "data_gap", "K 线存在缺口；停止开仓，已有持仓仍按可用新样本检查风险。", at)
                state["quality_blocked"] = True
            elif state["quality_blocked"]:
                state["quality_blocked"] = False
                state["last_signal_end"] = latest_end
                self.store.event(account, "data_recovered", "数据缺口已恢复；不补做恢复前信号。", at)
                can_open = False
            if can_open and account["status"] == "running" and latest_end > max(state["last_signal_end"], state["anchor"]):
                # If several bars arrived at once, they were missed rather than
                # observed in sequence. Record the interruption and skip orders.
                if len(new_confirmed) == 1:
                    side = self._signal(account, confirmed, sessions)
                    if side and (not account["position"] or account["position"]["side"] != side):
                        state["pending"] = {"side": side, "signal_time": confirmed[-1].time, "signal_end": latest_end,
                                            "data_time": data_cutoff, "observed_at": latest.received_at}
                        self.store.event(account, "signal", "已确认柱产生信号，等待之后的新鲜样本成交。", at, **state["pending"])
            state["last_signal_end"] = max(state["last_signal_end"], latest_end)
            self._mark(account, latest.close)
            self._stats(account)
            if account["status"] not in {"passed", "failed"} and account["equity"] <= account["drawdown_limit"]:
                self._close(account, latest.close, clock, at, "maximum_drawdown")
                self._end(account, "failed", at, "净值触及最大亏损线，考试失败。")
            if not account["position"] and account["status"] not in TERMINAL:
                self._flat_risk(account, at)
            if account["status"] == "running" and self._passing(account):
                self._end(account, "passed", at, "平仓后达到盈利目标、交易天数和一致性要求，通过考试。")
            if account["status"] == "running":
                account["run_state"] = ("daily_loss_pause" if state["halt_day"] == day else "market_closed" if closed
                                        else "data_gap" if gap else "waiting_for_post_start_data" if data_cutoff <= state.get("not_before", 0) else "running")
            state["last_data_clock"] = data_cutoff
            state["last_received_at"] = latest.received_at
            state["last_bar_time"] = latest.time
            state["last_risk_bar_end"] = max([state.get("last_risk_bar_end", 0), *[bar.end for bar in confirmed]])
            account["updated_at"] = at
            self.store.equity(account, clock)
            self.store.save(account)
            return self.store.public(account)
