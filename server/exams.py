"""Local, versioned paper evaluation accounts. No broker order APIs are used."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .ai_routes import local_write
from .models import now_ms


EXAM_TIMEFRAMES = ("1m", "3m", "5m", "15m", "30m", "1h")
STRATEGIES = [
    {"id": "ema-cross", "name": "EMA 20 / 60 交叉", "version": 1,
     "description": "基础执行策略，非 AI 看盘框架，未经本项目回测验证。已确认柱 EMA20 上穿 / 下穿 EMA60 时改变方向。",
     "parameters": {"fast_period": 20, "slow_period": 60}, "warmup_bars": 61,
     "execution_model": "confirmed_signal_then_observed_close"},
    {"id": "channel-breakout", "name": "20 柱收盘突破", "version": 1,
     "description": "基础执行策略，非 AI 看盘框架，未经本项目回测验证。已确认收盘突破此前 20 柱最高 / 最低价时改变方向。",
     "parameters": {"lookback": 20}, "warmup_bars": 21,
     "execution_model": "confirmed_signal_then_observed_close"},
]


class PresetDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="本项目模拟考试规则，可自行调整，不代表任何平台的官方规则。", max_length=600)
    initial_balance: float = Field(ge=1000, le=10_000_000)
    profit_target: float = Field(gt=0, le=1_000_000)
    max_drawdown: float = Field(gt=0, le=1_000_000)
    daily_loss_limit: float | None = Field(default=None, gt=0, le=1_000_000)
    max_contracts: int = Field(ge=1, le=100)
    min_trading_days: int = Field(default=2, ge=1, le=365)
    consistency_pct: float | None = Field(default=0.5, ge=0.1, le=1)
    drawdown_mode: Literal["eod_trailing", "fixed"] = "eod_trailing"
    fee_per_contract: float = Field(default=2.5, ge=0, le=1000)
    slippage_ticks: float = Field(default=1, ge=0, le=100)
    session_close: str = Field(default="15:10", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    session_reopen: str = Field(default="17:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    timezone: Literal["America/Chicago"] = "America/Chicago"

    @model_validator(mode="after")
    def compatible_rules(self):
        if self.max_drawdown >= self.initial_balance:
            raise ValueError("最大亏损必须小于初始余额")
        if self.daily_loss_limit is not None and self.daily_loss_limit > self.max_drawdown:
            raise ValueError("日亏损上限不能超过最大亏损")
        if self.session_close >= self.session_reopen:
            raise ValueError("停交易时间应早于重新开市时间")
        return self


class PresetUpdate(PresetDraft):
    expected_version: int = Field(ge=1)


class AccountCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=80)
    preset_id: str = Field(min_length=1, max_length=64)
    strategy_id: str = Field(min_length=1, max_length=64)
    symbol: str = Field(min_length=1, max_length=64)
    timeframe: str = "5m"
    quantity: int = Field(default=1, ge=1, le=100)

    @field_validator("timeframe")
    @classmethod
    def period(cls, value):
        if value not in EXAM_TIMEFRAMES:
            raise ValueError("考试仅支持 1m、3m、5m、15m、30m、1h")
        return value


class AccountAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["start", "pause", "resume", "terminate", "restart"]


class ExamStore:
    def __init__(self, store):
        self.store = store
        with store.lock, store.db:
            store.db.executescript("""
                CREATE TABLE IF NOT EXISTS exam_presets (
                    id TEXT NOT NULL, version INTEGER NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY (id, version));
                CREATE TABLE IF NOT EXISTS exam_accounts (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS exam_trades (
                    id TEXT PRIMARY KEY, account_id TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS exam_trades_account ON exam_trades(account_id);
                CREATE TABLE IF NOT EXISTS exam_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS exam_events_account ON exam_events(account_id);
                CREATE TABLE IF NOT EXISTS exam_equity (
                    account_id TEXT NOT NULL, time INTEGER NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY (account_id, time));
            """)
            for size, target, loss, contracts in [(50000, 3000, 2000, 5), (100000, 6000, 3000, 10), (150000, 9000, 4500, 15)]:
                key = f"combine-{size // 1000}k"
                if not store.db.execute("SELECT 1 FROM exam_presets WHERE id=?", (key,)).fetchone():
                    self._insert_preset(key, 1, PresetDraft(name=f"{size // 1000}K Combine", initial_balance=size,
                                        profit_target=target, max_drawdown=loss, max_contracts=contracts).model_dump())

    @staticmethod
    def encode(value):
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))

    def _insert_preset(self, key, version, value):
        item = {**value, "id": key, "version": version, "created_at": now_ms(), "rule_source": "project_simulation"}
        self.store.db.execute("INSERT INTO exam_presets VALUES (?,?,?)", (key, version, self.encode(item)))
        return item

    def presets(self):
        with self.store.lock:
            rows = self.store.db.execute("""SELECT p.payload FROM exam_presets p JOIN
                (SELECT id, MAX(version) AS version FROM exam_presets GROUP BY id) newest USING(id, version)
                ORDER BY json_extract(p.payload, '$.initial_balance'), p.id""").fetchall()
        return [json.loads(row[0]) for row in rows]

    def preset(self, key):
        with self.store.lock:
            row = self.store.db.execute("SELECT payload FROM exam_presets WHERE id=? ORDER BY version DESC LIMIT 1", (key,)).fetchone()
        if not row:
            raise HTTPException(404, "考试套餐不存在")
        return json.loads(row[0])

    def create_preset(self, body):
        with self.store.lock, self.store.db:
            return self._insert_preset(str(uuid4()), 1, body.model_dump())

    def update_preset(self, key, body):
        with self.store.lock, self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            old = self.preset(key)
            if old["version"] != body.expected_version:
                raise HTTPException(409, "规则已有新版本，请重新加载后再保存")
            return self._insert_preset(key, old["version"] + 1, body.model_dump(exclude={"expected_version"}))

    def save(self, account):
        self.store.db.execute("INSERT OR REPLACE INTO exam_accounts VALUES (?,?)", (account["id"], self.encode(account)))

    def get(self, key):
        with self.store.lock:
            row = self.store.db.execute("SELECT payload FROM exam_accounts WHERE id=?", (key,)).fetchone()
        if not row:
            raise HTTPException(404, "模拟考试账号不存在")
        return json.loads(row[0])

    def accounts(self):
        with self.store.lock:
            rows = self.store.db.execute("SELECT payload FROM exam_accounts ORDER BY json_extract(payload, '$.created_at') DESC, id").fetchall()
        return [json.loads(row[0]) for row in rows]

    @staticmethod
    def public(account):
        return {key: deepcopy(value) for key, value in account.items() if not key.startswith("_")}

    def event(self, account, kind, message, at, **details):
        value = {"time": at, "kind": kind, "message": message, **details}
        cursor = self.store.db.execute("INSERT INTO exam_events(account_id,payload) VALUES (?,?)", (account["id"], self.encode(value)))
        return {"id": cursor.lastrowid, **value}

    def trade(self, account, item):
        self.store.db.execute("INSERT OR REPLACE INTO exam_trades VALUES (?,?,?)", (item["id"], account["id"], self.encode(item)))

    def get_trade(self, key):
        row = self.store.db.execute("SELECT payload FROM exam_trades WHERE id=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def equity(self, account, at):
        item = {"time": at, "balance": account["balance"], "equity": account["equity"], "drawdown_limit": account["drawdown_limit"]}
        self.store.db.execute("INSERT OR REPLACE INTO exam_equity VALUES (?,?,?)", (account["id"], at, self.encode(item)))

    def detail(self, key):
        with self.store.lock:
            account = self.get(key)
            trades = [json.loads(row[0]) for row in self.store.db.execute("SELECT payload FROM exam_trades WHERE account_id=? ORDER BY rowid DESC LIMIT 2000", (key,))]
            events = [{"id": row[0], **json.loads(row[1])} for row in self.store.db.execute("SELECT id,payload FROM exam_events WHERE account_id=? ORDER BY id DESC LIMIT 1000", (key,))]
            curve = [json.loads(row[0]) for row in self.store.db.execute("SELECT payload FROM exam_equity WHERE account_id=? ORDER BY time DESC LIMIT 5000", (key,))]
        return {"account": self.public(account), "trades": trades, "events": events, "equity_curve": list(reversed(curve)),
                "limits": {"trades": 2000, "events": 1000, "equity_curve": 5000}}


def valid_instrument(item, at=None):
    """Only real dated USD futures with known contract economics are executable."""
    if item.get("asset_type") != "future" or item.get("currency") != "USD" or not item.get("symbol", "").startswith("ibkr:"):
        return False
    expiry = str(item.get("expiry", ""))
    today = datetime.fromtimestamp((at or now_ms()) / 1000, timezone.utc).strftime("%Y%m%d")
    if len(expiry) < 8 or not expiry[:8].isdigit() or expiry[:8] < today:
        return False
    try:
        return all(math.isfinite(float(item[key])) and 0 < float(item[key]) <= 1_000_000 for key in ("multiplier", "tick_size"))
    except (TypeError, ValueError, KeyError):
        return False


router = APIRouter(prefix="/api/v1/exams")


@router.get("/presets")
async def presets(request: Request):
    return request.app.state.exams.store.presets()


@router.post("/presets", status_code=201, dependencies=[Depends(local_write)])
async def create_preset(body: PresetDraft, request: Request):
    return request.app.state.exams.store.create_preset(body)


@router.put("/presets/{preset_id}", dependencies=[Depends(local_write)])
async def update_preset(preset_id: str, body: PresetUpdate, request: Request):
    return request.app.state.exams.store.update_preset(preset_id, body)


@router.get("/strategies")
async def strategies():
    return deepcopy(STRATEGIES)


@router.get("/instruments")
async def instruments(request: Request):
    return request.app.state.exams.instruments()


@router.get("/accounts")
async def accounts(request: Request):
    return [request.app.state.exams.store.public(item) for item in request.app.state.exams.store.accounts()]


@router.post("/accounts", status_code=201, dependencies=[Depends(local_write)])
async def create_account(body: AccountCreate, request: Request):
    return request.app.state.exams.create_account(body)


@router.get("/accounts/{account_id}")
async def account(account_id: str, request: Request):
    return request.app.state.exams.store.detail(account_id)


@router.post("/accounts/{account_id}/actions", dependencies=[Depends(local_write)])
async def action(account_id: str, body: AccountAction, request: Request):
    return request.app.state.exams.action(account_id, body.action)
