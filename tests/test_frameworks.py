import asyncio
from copy import deepcopy
import json
from uuid import uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from server.ai import AnalysisService, validate_result
from server.app import create_app
from server.frameworks import FrameworkDraft, FrameworkStore, FrameworkUpdate
from server.storage import Store
from test_ai import answer, configure, fill_market, service


def draft(**changes):
    value = dict(name="测试方法", description="仅用于测试", timeframes=["1h", "4h"], indicators=["ema20", "macd"],
                 steps=[dict(id="trend", title="趋势", condition="EMA20 高于 EMA60"),
                        dict(id="missing", title="缺少数据", condition="ATR 大于昨日")], output_style="简洁")
    return FrameworkDraft(**{**value, **changes})


def framework_answer(snapshot):
    result = answer(snapshot)
    ref = result["observations"][0]["evidence_refs"][0]
    result["framework_checks"] = [dict(step_id=step["id"], status="insufficient_data" if step["id"] == "missing" else "not_met",
                                       explanation="测试：数据不足" if step["id"] == "missing" else "测试条件未满足",
                                       evidence_refs=[] if step["id"] == "missing" else [ref]) for step in snapshot["framework"]["steps"]]
    return result


def test_versions_persist_without_overwriting_and_conflicts_are_atomic(tmp_path):
    path = tmp_path / "market.sqlite"
    store = Store(path)
    methods = FrameworkStore(store)
    first = methods.create(draft())
    updated = methods.update(first["id"], FrameworkUpdate(**draft(name="新方法").model_dump(), expected_version=1))
    assert updated["version"] == 2
    assert methods.get(first["id"], 1) == first
    with pytest.raises(HTTPException) as conflict:
        methods.update(first["id"], FrameworkUpdate(**draft(name="过时草稿").model_dump(), expected_version=1))
    assert conflict.value.status_code == 409
    assert len(methods.versions(first["id"])) == 2
    store.close()
    reopened = Store(path)
    try:
        methods = FrameworkStore(reopened)
        assert methods.get(first["id"]) == updated
        assert methods.get(first["id"], 1) == first
        assert len([m for m in methods.list() if m["id"].startswith("preset-")]) == 3
    finally:
        reopened.close()


@pytest.mark.parametrize("change", [
    {"name": "   "}, {"steps": []}, {"timeframes": ["1h"]}, {"timeframes": ["1h", "bogus"]},
    {"timeframes": ["1h", "1h"]}, {"indicators": ["invented"]},
    {"steps": [dict(id="x", title="a", condition="b"), dict(id="x", title="c", condition="d")]},
])
def test_invalid_framework_inputs_rejected(change):
    with pytest.raises(ValueError):
        draft(**change)


def test_custom_periods_and_framework_content_freeze_in_snapshot(service):
    first = service.frameworks.create(draft(timeframes=["1d", "4h"]))
    snap = service.capture("15m", framework_id=first["id"], framework_version=1)
    assert list(snap["timeframes"]) == ["15m", "4h", "1d"]
    service.frameworks.update(first["id"], FrameworkUpdate(**draft(name="后来改名").model_dump(), expected_version=1))
    assert service.store.snapshot(snap["id"])["framework"] == first
    historical = service.capture("1h", framework_id=first["id"], framework_version=1)
    assert historical["framework"] == first
    service.market.last_rest["1d"] = 0
    assert not service.capture("15m", framework_id=first["id"], framework_version=1)["ready"]


def test_checks_require_exact_coverage_evidence_and_explicit_missing_data(service):
    method = service.frameworks.create(draft())
    snap = service.capture("1h", framework_id=method["id"], framework_version=1)
    good = framework_answer(snap)
    checked = validate_result(json.dumps(good), snap, method)
    assert checked["framework_checks"][1]["status"] == "insufficient_data"
    assert checked["framework_checks"][0]["title"] == "趋势"
    reverse = deepcopy(good); reverse["framework_checks"].reverse()
    assert validate_result(json.dumps(reverse), snap, method)["framework_checks"][0]["step_id"] == "trend"
    for kind in ["missing", "duplicate", "unknown", "fake_evidence", "no_evidence", "invented_status"]:
        bad = deepcopy(good)
        if kind == "missing": bad["framework_checks"].pop()
        if kind == "duplicate": bad["framework_checks"][1] = deepcopy(bad["framework_checks"][0])
        if kind == "unknown": bad["framework_checks"][0]["step_id"] = "other"
        if kind == "fake_evidence": bad["framework_checks"][1]["evidence_refs"] = ["not-real"]
        if kind == "no_evidence": bad["framework_checks"][0]["evidence_refs"] = []
        if kind == "invented_status": bad["framework_checks"][0]["status"] = "guaranteed"
        with pytest.raises(ValueError): validate_result(json.dumps(bad), snap, method)


def test_analysis_followup_and_export_keep_original_method_after_edit(service):
    async def scenario():
        configure(service)
        method = service.frameworks.create(draft())
        snap = service.capture("1h", framework_id=method["id"], framework_version=1)
        class Fixture:
            seen = []
            async def generate(self, config, messages):
                self.seen.append(deepcopy(messages))
                return json.dumps(framework_answer(snap)), {"total_tokens": 20}
        fixture = Fixture(); service.adapter = fixture
        first_id = str(uuid4()); service.start(first_id, snap["id"], None, "分析")
        await service.tasks[first_id]
        assert service.public_run(first_id)["status"] == "completed"
        service.frameworks.update(method["id"], FrameworkUpdate(**draft(name="新版本", timeframes=["1d", "1w"]).model_dump(), expected_version=1))
        followup_id = str(uuid4()); service.start(followup_id, None, first_id, "追问")
        await service.tasks[followup_id]
        followup = service.public_run(followup_id)
        assert followup["status"] == "completed"
        assert followup["framework"] == method == followup["snapshot"]["framework"]
        assert followup["framework_version"] == method["id"] + "@1"
        assert "新版本" not in json.dumps(fixture.seen, ensure_ascii=False)
        assert fixture.seen[0][0]["role"] == "system"
        assert fixture.seen[0][2]["role"] == "user" and "本次固定看盘框架" in fixture.seen[0][2]["content"]
    asyncio.run(scenario())


def test_framework_api_permissions_version_selection_and_legacy_history(tmp_path):
    app = create_app(tmp_path, live=False)
    headers = {"X-Local-Market": "1"}
    with TestClient(app) as client:
        fill_market(app.state.market)
        body = draft().model_dump()
        assert client.post('/api/v1/ai/frameworks', json=body).status_code == 403
        assert client.post('/api/v1/ai/frameworks', json=body, headers={**headers, 'Origin': 'https://evil.example'}).status_code == 403
        created = client.post('/api/v1/ai/frameworks', json=body, headers=headers)
        assert created.status_code == 201
        method = created.json()
        capture = dict(symbol='BTC', timeframe='1h', framework_id=method['id'], framework_version=1)
        snap = client.post('/api/v1/ai/snapshots', json=capture, headers=headers).json()
        assert snap['framework']['version'] == 1 and snap['ready']
        assert client.post('/api/v1/ai/snapshots', json={**capture, 'framework_version': 999}, headers=headers).status_code == 404
        capture.pop('framework_version')
        assert client.post('/api/v1/ai/snapshots', json=capture, headers=headers).status_code == 422
        versions = client.get('/api/v1/ai/frameworks/'+method['id']+'/versions')
        assert versions.json() == [method] and versions.headers['cache-control'] == 'no-store'
        # Older records still have no framework snapshot and remain readable/exportable.
        service = app.state.ai
        legacy_snap = service.capture('1h')
        legacy = dict(id=str(uuid4()), snapshot_id=legacy_snap['id'], parent_id=None, created_at=1, updated_at=1,
                      status='completed', question='旧记录', symbol='BTC', timeframe='1h', framework_version='default-multitimeframe-v1')
        service.store.add_run(legacy)
        exported = client.get('/api/v1/ai/analyses/'+legacy['id']+'/export')
        assert exported.status_code == 200 and exported.json()['framework_version'] == 'default-multitimeframe-v1'
