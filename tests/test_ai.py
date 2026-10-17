"""Synthetic market/model fixtures only; never call a real model or use credentials."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
import stat
from uuid import uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient
import httpx
import pytest

from server.ai import AnalysisService, ChatAdapter, ModelFailure, model_snapshot, snapshot_market, validate_result
from server.ai_store import AnalysisStore, ConfigFile, ModelConfig
from server.app import create_app
from server.market import MarketService
from server.models import Candle, TIMEFRAMES, now_ms
from server.storage import Store


def fill_market(market):
    at = now_ms()
    for tf, step in TIMEFRAMES.items():
        latest = at // step
        bars = []
        for offset in range(-180, 1):
            start = (latest + offset) * step
            price = 80_000 + (offset + 180) * 3 + (offset % 7) * 2
            bars.append(Candle(tf, start, start + step, price, price + 20, price - 20,
                               price + 5, 50 + offset % 11, 100, "confirmed" if offset < 0 else "forming", at))
        market.store.merge(bars)
        market.last_rest[tf] = at
        market.needs_reconcile[tf] = False
        market.revisions[tf] += 1
    market._update_quote({"markPx": "80555", "prevDayPx": "80000", "dayNtlVlm": "100000",
                          "openInterest": "1000", "funding": "0.00001"}, "rest")


@pytest.fixture
def service(tmp_path):
    store = Store(tmp_path / "market.sqlite")
    market = MarketService(store)
    fill_market(market)
    service = AnalysisService(market, tmp_path)
    yield service
    store.close()


def answer(snapshot):
    periods = list(snapshot["timeframes"])
    refs = [snapshot["timeframes"][tf]["indicators"][-1]["evidence_id"] for tf in periods[:2]]
    return {"market_regime": "unclear", "thesis": "测试夹具：仅验证功能，不是真实模型分析。",
            "observations": [{"text": f"测试观察 {i}", "evidence_refs": [ref]} for i, ref in enumerate(refs)],
            "key_levels": [{"level_ref": snapshot["levels"][0]["level_ref"], "rationale": "测试窗口高点"}],
            "scenarios": [{"direction": direction, "trigger": "测试触发条件", "invalidation": "测试失效条件", "evidence_refs": refs}
                          for direction in ["bullish", "bearish"]],
            "risks": ["这是合成数据测试夹具。"], "follow_up_questions": ["测试多周期差异？"]}


def configure(service):
    service.config.save(ModelConfig(base_url="https://model.example/v1", model="fixture-model", api_key="fixture-secret"))


def test_snapshot_excludes_forming_uses_full_warmup_and_freezes(service):
    snap = service.capture("1h")
    assert snap["ready"]
    assert list(snap["timeframes"]) == ["15m", "1h", "4h", "1d"]
    for tf, data in snap["timeframes"].items():
        assert len(data["bars"]) == 120
        assert len(data["indicators"]) == 20
        assert all(b["status"] == "confirmed" and b["end"] <= snap["as_of"] for b in data["bars"])
        original = service.market.features(tf)[1][-2]
        assert data["latest_indicators"]["ema60"] == original["ema60"]
        assert data["latest_indicators"]["samples"] == 180
    frozen = deepcopy(snap)
    compact = model_snapshot(snap)
    for tf, data in compact["timeframes"].items():
        unpacked = dict(zip(data["bar_columns"], data["bars"][-1]))
        assert all(snap["timeframes"][tf]["bars"][-1][key] == value for key, value in unpacked.items())
        indicators = dict(zip(data["indicator_columns"], data["indicators"][-1]))
        assert indicators["ema60"] == snap["timeframes"][tf]["latest_indicators"]["ema60"]
    assert len(json.dumps(compact)) < len(json.dumps(snap)) * 0.6
    latest = service.market.store.bars("1h")[-2]
    service.market.store.merge([replace(latest, close=latest.close + 1)])
    service.market.quote["mark"] += 200
    assert service.store.snapshot(snap["id"]) == frozen


@pytest.mark.parametrize("tf", list(TIMEFRAMES))
def test_every_timeframe_has_valid_multiperiod_context(service, tf):
    snap = snapshot_market(service.market, tf)
    assert snap["ready"] and tf in snap["timeframes"]
    assert len(snap["timeframes"]) >= 2
    validate_result(json.dumps(answer(snap)), snap)


def test_stale_missing_and_warmup_prevent_analysis(service):
    service.market.last_rest["1h"] = 0
    assert not service.capture("1h")["ready"]
    service.market.last_rest["1h"] = now_ms()
    bar = service.market.store.bars("4h")[-25]
    service.market.store.db.execute("DELETE FROM candles WHERE timeframe=? AND time=?", ("4h", bar.time))
    service.market.store.db.commit()
    service.market.revisions["4h"] += 1
    snap = service.capture("1h")
    assert not snap["ready"]
    assert any("预热" in message for message in snap["blocking_reasons"])


def test_result_rejects_unknown_refs_levels_and_single_timeframe(service):
    snap = service.capture("1h")
    good = answer(snap)
    checked = validate_result(json.dumps(good), snap)
    assert checked["key_levels"][0]["price"] == snap["levels"][0]["price"]
    for field in ("unknown_ref", "unknown_level", "one_period", "same_direction", "invented_price"):
        bad = deepcopy(good)
        if field == "unknown_ref": bad["observations"][0]["evidence_refs"] = ["made-up"]
        if field == "unknown_level": bad["key_levels"][0]["level_ref"] = "made-up"
        if field == "one_period": bad["observations"][1]["evidence_refs"] = bad["observations"][0]["evidence_refs"]
        if field == "same_direction": bad["scenarios"][1]["direction"] = "bullish"
        if field == "invented_price": bad["key_levels"][0]["price"] = 123
        with pytest.raises(ValueError): validate_result(json.dumps(bad), snap)


def test_config_private_file_and_credential_destination_binding(tmp_path):
    file = ConfigFile(tmp_path)
    assert not file.read().ready
    config = ModelConfig(base_url="https://model.example/v1/", model="model", api_key="secret")
    saved = file.save(config)
    assert saved["ready"] and saved["has_key"] and "secret" not in json.dumps(saved)
    assert stat.S_IMODE(file.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(file.folder.stat().st_mode) == 0o700
    file.save(ModelConfig(base_url=config.base_url, model="second"))
    assert file.read().api_key == "secret"
    file.save(ModelConfig(base_url="https://different.example/v1", model="second"))
    assert not file.read().api_key and not file.read().ready
    for url in ["http://remote.example/v1", "https://user:pass@host/v1", "https://host/v1?key=abc", "file:///tmp/x"]:
        with pytest.raises(ValueError): ModelConfig(base_url=url)


def test_adapter_protocol_success_and_redacted_errors():
    config = ModelConfig(base_url="https://model.example/v1", model="fixture", api_key="fixture-secret")
    calls = []
    def handler(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer fixture-secret"
        assert str(request.url) == "https://model.example/v1/chat/completions"
        body = json.loads(request.content)
        assert body["model"] == "fixture" and body["max_completion_tokens"] == 4096 and body["stream"] is False
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}], "usage": {"total_tokens": 15}})
    adapter = ChatAdapter(httpx.MockTransport(handler))
    assert asyncio.run(adapter.generate(config, [{"role": "user", "content": "test"}])) == ("{}", {"total_tokens": 15})
    assert len(calls) == 1
    for code in [302, 401, 403, 429, 500]:
        adapter = ChatAdapter(httpx.MockTransport(lambda request: httpx.Response(code, text="fixture-secret provider-internals")))
        with pytest.raises(ModelFailure) as caught:
            asyncio.run(adapter.generate(config, []))
        assert "fixture-secret" not in str(caught.value) and "provider-internals" not in str(caught.value)


def test_full_pipeline_followup_idempotency_and_persistence(service):
    async def scenario():
        configure(service)
        snapshot = service.capture("1h")
        seen = []
        def handler(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer(snapshot))}, "finish_reason": "stop"}], "usage": {"total_tokens": 100}})
        service.adapter = ChatAdapter(httpx.MockTransport(handler))
        run_id = str(uuid4())
        started = service.start(run_id, snapshot["id"], None, "分析 BTC")
        assert service.start(run_id, snapshot["id"], None, "分析 BTC") == started
        with pytest.raises(HTTPException): service.start(str(uuid4()), snapshot["id"], None, "重复")
        await service.tasks[run_id]
        first = service.public_run(run_id)
        assert first["status"] == "completed" and first["usage"]["total_tokens"] == 100
        assert "fixture-secret" not in json.dumps(first)
        service.market.quote["mark"] = 99999
        second_id = str(uuid4())
        service.start(second_id, snapshot["id"], run_id, "解释周期冲突")
        await service.tasks[second_id]
        second = service.public_run(second_id)
        assert second["snapshot"] == snapshot == first["snapshot"]
        assert second["snapshot_hash"] == first["snapshot_hash"]
        assert seen[1]["messages"][-3]["content"] == "分析 BTC"
        assert json.loads(seen[1]["messages"][-2]["content"])["thesis"].startswith("测试夹具")
        assert len(service.store.list_runs()) == 2
        reopened = Store(service.market.store.path)
        try:
            persisted = AnalysisStore(reopened)
            assert persisted.run(second_id)["status"] == "completed"
            assert persisted.snapshot(snapshot["id"]) == snapshot
        finally: reopened.close()
    asyncio.run(scenario())


def test_unconfigured_expired_and_unready_do_not_call_model(service):
    snap = service.capture("1h")
    with pytest.raises(HTTPException, match="API"):
        service.start(str(uuid4()), snap["id"], None, "分析")
    configure(service)
    snap["id"] = str(uuid4()); snap["as_of"] -= 180_000
    service.store.add_snapshot(snap)
    with pytest.raises(HTTPException, match="两分钟"):
        service.start(str(uuid4()), snap["id"], None, "分析")
    service.market.last_rest["1h"] = 0
    bad = service.capture("1h")
    with pytest.raises(HTTPException, match="尚未就绪"):
        service.start(str(uuid4()), bad["id"], None, "分析")
    assert not service.store.list_runs()


def test_invalid_output_repair_is_bounded_and_preserved(service):
    async def scenario():
        configure(service)
        snap = service.capture("1h")
        class Invalid:
            async def generate(self, config, messages): return "not-json", {"total_tokens": 10}
        service.adapter = Invalid()
        run_id = str(uuid4())
        service.start(run_id, snap["id"], None, "分析")
        await service.tasks[run_id]
        result = service.public_run(run_id)
        assert result["status"] == "failed" and result["result"] is None
        assert len(result["attempts"]) == 2 and result["raw_output"] == "not-json"
        assert result["usage"]["total_tokens"] == 20
        assert "repair_messages" in result
    asyncio.run(scenario())


def test_cancel_timeout_and_restart_have_terminal_states(service):
    async def scenario():
        configure(service)
        snap = service.capture("1h")
        class Slow:
            async def generate(self, config, messages):
                await asyncio.Event().wait()
        service.adapter = Slow()
        run_id = str(uuid4())
        service.start(run_id, snap["id"], None, "取消")
        assert (await service.cancel(run_id))["status"] == "cancelled"
        service.timeout_seconds = 0.01
        run_id = str(uuid4())
        service.start(run_id, snap["id"], None, "超时")
        await service.tasks[run_id]
        assert service.public_run(run_id)["status"] == "failed"
        old = service.store.run(run_id)
        old["status"] = "running"
        service.store.update(old)
        restored = AnalysisStore(service.market.store)
        assert restored.run(run_id)["status"] == "failed"
        assert "重启" in restored.run(run_id)["error"]
    asyncio.run(scenario())


def test_local_api_permissions_config_and_real_snapshot(tmp_path):
    app = create_app(tmp_path, live=False)
    with TestClient(app) as client:
        fill_market(app.state.market)
        assert not client.get("/api/v1/ai/config").json()["ready"]
        assert client.put("/api/v1/ai/config", json={}).status_code == 403
        headers = {"X-Local-Market": "1"}
        assert client.put("/api/v1/ai/config", json={}, headers={**headers, "Origin": "https://evil.example"}).status_code == 403
        response = client.put("/api/v1/ai/config", json={"base_url": "http://remote.example", "api_key": "DO-NOT-REFLECT"}, headers=headers)
        assert response.status_code == 422 and "DO-NOT-REFLECT" not in response.text
        snap = client.post("/api/v1/ai/snapshots", json={"timeframe": "4h"}, headers=headers).json()
        assert snap["ready"] and snap["timeframe"] == "4h"
        assert client.post("/api/v1/ai/analyses", json={"request_id": str(uuid4()), "snapshot_id": snap["id"], "question": "分析"}, headers=headers).status_code == 409
        assert client.get("/api/v1/ai/config").headers["cache-control"] == "no-store"
