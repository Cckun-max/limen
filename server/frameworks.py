"""Versioned local analysis methods; published versions are immutable."""
import json
from typing import Literal
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import TIMEFRAMES, now_ms


class FrameworkStep(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    title: str = Field(min_length=1, max_length=80)
    condition: str = Field(min_length=1, max_length=1200)


class FrameworkDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=600)
    timeframes: list[str] = Field(default_factory=list, max_length=4)
    indicators: list[Literal["ema20", "ema60", "rsi14", "macd", "volume"]] = Field(default_factory=list, max_length=5)
    steps: list[FrameworkStep] = Field(min_length=1, max_length=12)
    output_style: str = Field(default="简洁中文，先给结论，再说明依据、分歧与失效条件。", max_length=1200)

    @field_validator("timeframes")
    @classmethod
    def periods(cls, value):
        if len(set(value)) != len(value) or any(tf not in TIMEFRAMES for tf in value) or len(value) == 1:
            raise ValueError("选择 2–4 个周期，或留空跟随图表自动搭配")
        return sorted(value, key=TIMEFRAMES.get)

    @field_validator("indicators")
    @classmethod
    def unique_indicators(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("指标不能重复")
        return value

    @field_validator("steps")
    @classmethod
    def unique_steps(cls, value):
        if len({step.id for step in value}) != len(value):
            raise ValueError("步骤编号不能重复")
        return value


class FrameworkUpdate(FrameworkDraft):
    expected_version: int = Field(ge=1)


PRESETS = [
    ("multitimeframe", "多周期共振", "先看大周期，再检查短周期方向和量价是否配合。", [
        ("trend", "大周期趋势", "较大周期的收盘价高于 EMA20 和 EMA60，且 EMA20 高于 EMA60。判断是否满足偏多趋势条件，同时说明偏空或冲突状态。"),
        ("momentum", "跨周期动量", "至少两个周期的 MACD 柱为正且 RSI14 高于 50。说明哪些周期满足，哪些周期冲突。"),
        ("volume", "量价配合", "最近已确认上涨柱的成交量高于此前 5 根确认柱均量。按可用原始成交量比较，不编造缺失数据。"),
    ]),
    ("trend", "趋势跟随", "观察均线方向、回踩与趋势恢复，分别给出多空条件。", [
        ("direction", "趋势方向", "主周期 EMA20 高于 EMA60 且最近已确认收盘价在 EMA20 上方。说明该偏多条件是否满足。"),
        ("pullback", "回踩与恢复", "最近 5 根主周期确认柱中曾触及或跌破 EMA20，最新确认收盘重新站上 EMA20。缺少对应均线数据时明确标记数据不足。"),
        ("confirmation", "动量确认", "主周期 MACD 柱为正、RSI14 高于 50，且较大周期未明显相反。列出冲突与失效观察条件。"),
    ]),
    ("range", "震荡观察", "检查区间特征，区分窗口高低点与已验证支撑阻力。", [
        ("range", "区间条件", "主周期 RSI14 在 40–60 之间，EMA20 与 EMA60 的绝对差不超过最新确认收盘价的 0.5%。"),
        ("boundary", "接近区间边缘", "主周期最新确认收盘处于最近 20 根确认 K 线高低区间的上端或下端 20% 内。说明位置，不能自动认定极值为有效支撑阻力。"),
        ("breakout", "突破警戒", "最新主周期确认柱收盘越过此前 19 根的最高或最低价，且成交量超过此前 5 根均量。检查是否出现区间失效迹象。"),
    ]),
]


class FrameworkStore:
    def __init__(self, store):
        self.store = store
        with store.lock, store.db:
            store.db.executescript("""
                CREATE TABLE IF NOT EXISTS ai_framework_versions (
                    framework_id TEXT NOT NULL, version INTEGER NOT NULL,
                    created_at INTEGER NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY (framework_id, version)
                );
            """)
            for slug, name, description, steps in PRESETS:
                framework_id = "preset-" + slug
                if store.db.execute("SELECT 1 FROM ai_framework_versions WHERE framework_id=?", (framework_id,)).fetchone():
                    continue
                draft = FrameworkDraft(name=name, description=description, indicators=["ema20", "ema60", "rsi14", "macd", "volume"],
                                       steps=[dict(id=key, title=title, condition=condition) for key, title, condition in steps])
                self._insert(framework_id, 1, draft.model_dump())

    def _insert(self, framework_id, version, draft):
        item = {**draft, "id": framework_id, "version": version, "created_at": now_ms()}
        self.store.db.execute("INSERT INTO ai_framework_versions VALUES (?,?,?,?)",
                              (framework_id, version, item["created_at"], json.dumps(item, ensure_ascii=False)))
        return item

    def list(self):
        with self.store.lock:
            rows = self.store.db.execute("""SELECT v.payload FROM ai_framework_versions v
                JOIN (SELECT framework_id, MAX(version) AS version FROM ai_framework_versions GROUP BY framework_id) latest
                USING (framework_id, version) ORDER BY v.framework_id""").fetchall()
        return [json.loads(row[0]) for row in rows]

    def get(self, framework_id, version=None):
        with self.store.lock:
            if version is None:
                row = self.store.db.execute("SELECT payload FROM ai_framework_versions WHERE framework_id=? ORDER BY version DESC LIMIT 1", (framework_id,)).fetchone()
            else:
                row = self.store.db.execute("SELECT payload FROM ai_framework_versions WHERE framework_id=? AND version=?", (framework_id, version)).fetchone()
        if not row:
            raise HTTPException(404, "看盘框架或版本不存在")
        return json.loads(row[0])

    def versions(self, framework_id):
        self.get(framework_id)
        with self.store.lock:
            rows = self.store.db.execute("SELECT payload FROM ai_framework_versions WHERE framework_id=? ORDER BY version DESC", (framework_id,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def create(self, draft: FrameworkDraft):
        with self.store.lock, self.store.db:
            return self._insert(str(uuid4()), 1, draft.model_dump())

    def update(self, framework_id, draft: FrameworkUpdate):
        with self.store.lock, self.store.db:
            # Serialize revision publication across application processes as well as threads.
            self.store.db.execute("BEGIN IMMEDIATE")
            current = self.get(framework_id)
            if current["version"] != draft.expected_version:
                raise HTTPException(409, "框架已有新版本，请关闭编辑器并重新打开后再保存；当前草稿仍在编辑器中。")
            return self._insert(framework_id, current["version"] + 1, draft.model_dump(exclude={"expected_version"}))
