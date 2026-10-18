"""Deterministic accounting, causality and API boundaries for paper evaluations."""
import asyncio
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from server.app import create_app
from server.exam_runtime import ExamService
from server.exams import AccountCreate, PresetDraft, PresetUpdate, valid_instrument
from server.models import Candle
from server.storage import Store


BASE = int(datetime(2026, 10, 19, 9, tzinfo=ZoneInfo("America/Chicago")).timestamp() * 1000)
MINUTE = 60000


def contract(**changes):
    return {"symbol": "ibkr:123", "display_symbol": "MESZ6", "native_symbol": "MES", "name": "Micro E-mini S&P 500",
            "asset_type": "future", "currency": "USD", "expiry": "20991231", "multiplier": "5", "tick_size": .25,
            "exchange": "CME", "provider": "ibkr", **changes}


class Hub:
    live = False

    def __init__(self):
        self.broker = SimpleNamespace(instruments={"ibkr:123": contract()})
        self.broker.rows = lambda: list(self.broker.instruments.values())
        self.acquired, self.released, self.fail = [], [], False
        self.feed = SimpleNamespace(sessions=None, chart=lambda tf: self.snapshot)
        self.snapshot = {}

    async def acquire(self, symbol):
        if self.fail:
            raise HTTPException(429, "行情额度已满")
        self.acquired.append(symbol)
        return self.feed

    def release(self, symbol):
        self.released.append(symbol)


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr("server.exam_runtime.now_ms", lambda: BASE - 1000)
    store = Store(tmp_path / "exams.sqlite")
    result = ExamService(store, Hub())
    yield result
    asyncio.run(result.stop())
    store.close()


def new_account(service, *, rules=None, quantity=1, name="策略考试"):
    preset = "combine-50k"
    if rules:
        defaults = {"name": "测试规则", "initial_balance": 50000, "profit_target": 3000,
                    "max_drawdown": 2000, "max_contracts": 5, **rules}
        preset = service.store.create_preset(PresetDraft(**defaults))["id"]
    account = service.create_account(AccountCreate(name=name, preset_id=preset, strategy_id="channel-breakout",
                                                  symbol="ibkr:123", timeframe="1m", quantity=quantity), at=BASE - 2000)
    service.action(account["id"], "start")
    return account["id"]


def observation(count, *, seconds=3, price=100, closes=None, low=None, high=None, forming=True):
    cutoff = BASE + count * MINUTE + seconds * 1000
    values = closes or {}
    bars = []
    for index in range(count):
        close = values.get(index, 100)
        bars.append(Candle("1m", BASE + index * MINUTE, BASE + (index + 1) * MINUTE,
                           close, close + 1, close - 1, close, 10, 1, "confirmed", cutoff,
                           source="ibkr_history", symbol="ibkr:123"))
    if forming:
        bars.append(Candle("1m", BASE + count * MINUTE, BASE + (count + 1) * MINUTE,
                           price, high if high is not None else price + 1, low if low is not None else price - 1,
                           price, 10, 1, "forming", cutoff, source="ibkr_history", symbol="ibkr:123"))
    return bars, cutoff


def observe(service, key, count, **kwargs):
    quality = kwargs.pop("quality", {"status": "fresh"})
    bars, cutoff = observation(count, **kwargs)
    return service.step(key, bars, at=cutoff, data_cutoff=cutoff, quality=quality)


def open_long(service, key, *, price=111):
    observe(service, key, 20)
    signal = observe(service, key, 21, price=110, closes={20: 110})
    assert signal["position"] is None  # signal is known; no same-observation fill
    assert service.store.get(key)["_state"]["pending"]["side"] == "long"
    return observe(service, key, 21, seconds=20, price=price, closes={20: 110})


def test_actual_signal_waits_for_later_observation_and_ledger_costs(service):
    key = new_account(service, quantity=2)
    untouched = new_account(service, name="独立账号")
    item = open_long(service, key)
    assert item["position"]["entry_price"] == 111.25
    assert item["balance"] == 49995  # two contracts, one-side entry fee
    assert item["equity"] == 49992.5  # multiplier and adverse slippage
    bars, cutoff = observation(21, seconds=20, price=111, closes={20: 110})
    assert service.step(key, bars, at=cutoff, data_cutoff=cutoff)["trade_count"] == 1
    service.action(key, "pause")
    assert service.store.get(key)["position"] is not None
    done = observe(service, key, 21, seconds=40, price=115, closes={20: 110})
    assert done["status"] == "paused" and done["position"] is None
    trade = service.store.detail(key)["trades"][0]
    assert trade["gross_pnl"] == 35
    assert trade["net_pnl"] == 25
    assert done["balance"] == 50025 and done["total_fees"] == 10 and done["total_slippage"] == 5
    assert service.store.get(untouched)["balance"] == 50000
    assert trade["entry_time"] > trade["signal"]["data_time"]


def test_old_confirmed_correction_is_not_a_new_fill(service):
    key = new_account(service)
    observe(service, key, 20)
    # Only closed bars: the confirmed signal's closing price is already past.
    bars, cutoff = observation(21, forming=False, closes={20: 110})
    service.step(key, bars, at=cutoff, data_cutoff=cutoff)
    corrected = deepcopy(bars)
    corrected[-1].received_at += 5000
    corrected[-1].close = 110.5
    result = service.step(key, corrected, at=cutoff + 5000, data_cutoff=cutoff + 5000)
    assert result["position"] is None and result["trade_count"] == 0


def test_multi_bar_catchup_invalidates_pending_before_execution(service):
    key = new_account(service)
    observe(service, key, 20)
    observe(service, key, 21, price=110, closes={20: 110})
    result = observe(service, key, 24, price=120, closes={20: 110, 21: 111, 22: 115, 23: 119})
    assert result["trade_count"] == 0 and result["position"] is None
    assert any(item["kind"] == "missed_bars" for item in service.store.detail(key)["events"])


def test_intrabar_floating_loss_precedes_recovered_close_and_profit(service):
    key = new_account(service, rules={"max_drawdown": 100, "fee_per_contract": 0, "slippage_ticks": 0})
    open_long(service, key, price=111)
    result = observe(service, key, 22, price=120, low=80, closes={20: 110, 21: 111})
    assert result["status"] == "failed" and result["position"] is None
    assert result["balance"] == 49845  # low 80, not recovered close 120
    assert service.store.detail(key)["trades"][0]["exit_reason"] == "maximum_drawdown"


def test_entry_bar_pre_entry_low_cannot_retroactively_stop_new_position(service):
    key = new_account(service, rules={"max_drawdown": 100, "fee_per_contract": 0, "slippage_ticks": 0})
    open_long(service, key)
    result = observe(service, key, 21, seconds=40, price=112, low=70, closes={20: 110})
    assert result["status"] == "running" and result["position"] is not None


def test_confirmed_only_bar_extreme_is_checked_once(service):
    key = new_account(service, rules={"max_drawdown": 100, "fee_per_contract": 0, "slippage_ticks": 0})
    open_long(service, key)
    bars, cutoff = observation(23, forming=False, closes={20: 110, 21: 111, 22: 112})
    bars[-1].low = 80
    result = service.step(key, bars, at=cutoff, data_cutoff=cutoff)
    assert result["status"] == "failed"
    assert service.step(key, bars, at=cutoff, data_cutoff=cutoff)["trade_count"] == 1


def test_confirmation_has_independent_risk_cursor(service):
    key = new_account(service, rules={"max_drawdown": 100, "fee_per_contract": 0, "slippage_ticks": 0})
    open_long(service, key)
    bars, cutoff = observation(22, seconds=59, price=112, closes={20: 110, 21: 111})
    service.step(key, bars, at=cutoff, data_cutoff=cutoff)
    # Final low arrived with confirmation after the price clock passed the end.
    raw = service.store.get(key)
    raw["_state"]["last_data_clock"] = BASE + 23 * MINUTE + 1000
    with service.store.store.lock, service.store.store.db:
        service.store.save(raw)
    final, final_at = observation(23, seconds=3, forming=False, closes={20: 110, 21: 111, 22: 112})
    final[-1].low = 80
    result = service.step(key, final, at=final_at, data_cutoff=final_at)
    assert result["status"] == "failed" and result["position"] is None


def test_daily_loss_flatten_halts_today_without_failing_and_resumes_next_day(service):
    key = new_account(service, rules={"daily_loss_limit": 100, "fee_per_contract": 0, "slippage_ticks": 0})
    open_long(service, key)
    result = observe(service, key, 22, price=100, low=90, closes={20: 110, 21: 111})
    assert result["status"] == "running" and result["run_state"] == "daily_loss_pause"
    assert result["position"] is None and result["daily_pnl"] == -105
    assert observe(service, key, 23, price=130, closes={20: 110, 21: 111, 22: 120})["trade_count"] == 1
    bars, cutoff = observation(22, price=100)
    for bar in bars:
        bar.time += 86400000; bar.end += 86400000; bar.received_at += 86400000
    result = service.step(key, bars, at=cutoff + 86400000, data_cutoff=cutoff + 86400000)
    assert result["daily_pnl"] == 0 and result["run_state"] != "daily_loss_pause"


def test_rules_strategy_and_contract_snapshots_survive_edit_and_restart(service):
    key = new_account(service)
    original = service.store.get(key)
    rules = PresetDraft(**{k: v for k, v in original["preset"].items() if k in PresetDraft.model_fields})
    updated = service.store.update_preset(rules_id := original["preset"]["id"], PresetUpdate(**{**rules.model_dump(), "profit_target": 7000}, expected_version=1))
    assert updated["version"] == 2
    with pytest.raises(HTTPException) as error:
        service.store.update_preset(rules_id, PresetUpdate(**rules.model_dump(), expected_version=1))
    assert error.value.status_code == 409
    service.hub.broker.instruments["ibkr:123"]["multiplier"] = "50"
    repeat = service.action(key, "restart")
    assert repeat["id"] != key and repeat["parent_account_id"] == key
    assert repeat["preset"] == original["preset"] and repeat["strategy"] == original["strategy"]
    assert repeat["instrument"]["multiplier"] == 5 and repeat["trade_count"] == 0
    assert service.store.get(key)["preset"]["version"] == 1


def test_restart_and_stale_data_preserve_position_without_fabricated_exit(service):
    key = new_account(service)
    open_long(service, key)
    old = service.store.get(key)
    recovered = ExamService(service.store.store, service.hub)
    raw, cutoff = observation(21, seconds=40, price=113, closes={20: 110})
    result = recovered.step(key, raw, at=cutoff, data_cutoff=cutoff, quality={"status": "stale"})
    assert result["position"] == old["position"] and result["trade_count"] == 1
    assert result["run_state"] == "waiting_for_data"
    service.action(key, "pause")
    service.step(key, [], at=cutoff, data_cutoff=cutoff)
    assert service.store.get(key)["position"] is not None
    result = observe(service, key, 21, seconds=50, price=114, closes={20: 110})
    assert result["status"] == "paused" and result["position"] is None
    assert any(event["kind"] == "interrupted" for event in service.store.detail(key)["events"])


def test_missed_session_close_preserves_position_then_flattens_at_next_fresh_sample(service):
    key = new_account(service)
    opened = open_long(service, key)
    bars, _ = observation(21, seconds=20, price=111, closes={20: 110})
    close_due = BASE + (6 * 60 + 20) * MINUTE  # Monday 15:20 Chicago, beyond the 15:10 cutoff
    result = service.step(key, bars, at=close_due, data_cutoff=close_due, quality={"status": "stale"})
    assert result["position"] == opened["position"]  # no fabricated old-price exit
    assert result["run_state"] == "closing" and result["status"] == "running"
    assert service.store.detail(key)["trades"][0]["status"] == "open"
    assert any(event["kind"] == "missed_session_close" for event in service.store.detail(key)["events"])
    fresh, next_at = observation(22, price=112, closes={20: 110, 21: 111})
    for bar in fresh:
        bar.time += 86400000
        bar.end += 86400000
        bar.received_at += 86400000
    result = service.step(key, fresh, at=next_at + 86400000, data_cutoff=next_at + 86400000)
    assert result["position"] is None and result["trade_count"] == 1
    trade = service.store.detail(key)["trades"][0]
    assert trade["status"] == "closed" and trade["exit_reason"] == "missed_session_close"
    assert trade["exit_raw_price"] == 112 and trade["exit_time"] == next_at + 86400000
    assert service.store.get(key)["_state"]["pending"] is None  # first recovery sample cannot reopen


def test_delayed_pre_start_history_cannot_generate_trades(service, monkeypatch):
    monkeypatch.setattr("server.exam_runtime.now_ms", lambda: BASE + 30 * MINUTE)
    key = new_account(service)
    observe(service, key, 20, quality={"status": "delayed", "history_delay_minutes": 15})
    result = observe(service, key, 21, price=110, closes={20: 110}, quality={"status": "delayed", "history_delay_minutes": 15})
    assert result["run_state"] == "waiting_for_post_start_data"
    assert result["trade_count"] == 0 and service.store.get(key)["_state"]["pending"] is None


@pytest.mark.parametrize("mode,expected", [("fixed", 48000), ("eod_trailing", 49000)])
def test_drawdown_high_water_moves_at_daily_settlement_only(service, mode, expected):
    key = new_account(service, rules={"drawdown_mode": mode, "fee_per_contract": 0, "slippage_ticks": 0})
    open_long(service, key)
    # Position profit alone does not move the end-of-day trailing line.
    result = observe(service, key, 21, seconds=40, price=311, closes={20: 110})
    assert result["drawdown_limit"] == 48000
    raw = service.store.get(key)
    with service.store.store.lock, service.store.store.db:
        service._close(raw, 311, BASE + 22 * MINUTE, BASE + 22 * MINUTE, "test")
        service._eod(raw, BASE + 22 * MINUTE)
        service.store.save(raw)
    assert service.store.get(key)["drawdown_limit"] == expected


def test_pass_requires_flat_goal_trading_days_and_consistency(service):
    key = new_account(service, rules={"profit_target": 100, "min_trading_days": 2, "consistency_pct": .5})
    raw = service.store.get(key)
    raw.update(balance=50200, equity=50200)
    raw["_state"]["days"] = {"20261019": {"net_pnl": 150, "opened": True}, "20261020": {"net_pnl": 50, "opened": True}}
    service._stats(raw)
    assert not service._passing(raw)
    raw["_state"]["days"]["20261019"]["net_pnl"] = 100
    raw["_state"]["days"]["20261020"]["net_pnl"] = 100
    service._stats(raw)
    assert service._passing(raw)
    raw["position"] = {"side": "long"}
    assert not service._passing(raw)


def test_background_leases_shared_retried_and_released_without_page(service):
    first, second = new_account(service), new_account(service)
    bars, cutoff = observation(20)
    service.hub.snapshot = {"bars": [bar.public() for bar in bars], "quality": {"status": "fresh", "data_cutoff": cutoff}}
    async def scenario():
        service.hub.fail = True
        await service.tick(at=cutoff)
        assert service.store.get(first)["run_state"] == "waiting_for_feed"
        service.hub.fail = False
        await service.tick(at=cutoff)
        assert service.hub.acquired == ["ibkr:123"]
        service.action(first, "pause")
        await service.tick(at=cutoff)
        assert not service.hub.released
        service.action(second, "terminate")
        await service.tick(at=cutoff)
        assert service.hub.released == ["ibkr:123"]
    asyncio.run(scenario())


@pytest.mark.parametrize("field,value", [("profit_target", float("nan")), ("fee_per_contract", float("inf")),
                                        ("slippage_ticks", -1), ("max_drawdown", 50000), ("session_close", "25:00")])
def test_financial_inputs_are_finite_and_bounded(field, value):
    with pytest.raises(ValueError):
        PresetDraft(**{"name": "test", "initial_balance": 50000, "profit_target": 3000,
                       "max_drawdown": 2000, "max_contracts": 5, field: value})


def test_only_unexpired_usd_dated_futures_are_supported(service):
    assert valid_instrument(contract())
    for changes in [{"multiplier": "nan"}, {"tick_size": 0}, {"asset_type": "stock"}, {"expiry": "202610"}, {"expiry": "20000101"}]:
        service.hub.broker.instruments["ibkr:123"] = contract(**changes)
        with pytest.raises(HTTPException) as error:
            new_account(service)
        assert error.value.status_code == 422


def test_exam_api_local_write_permissions_and_roundtrip(tmp_path):
    with TestClient(create_app(tmp_path, live=False)) as client:
        presets = client.get("/api/v1/exams/presets").json()
        assert len(presets) == 3
        assert len(client.get("/api/v1/exams/strategies").json()) == 2
        client.app.state.hub.broker.instruments["ibkr:123"] = contract()
        body = {"name": "API考试", "preset_id": presets[0]["id"], "strategy_id": "ema-cross", "symbol": "ibkr:123", "timeframe": "5m"}
        headers = {"x-local-market": "1"}
        assert client.post("/api/v1/exams/accounts", json=body).status_code == 403
        assert client.post("/api/v1/exams/accounts", json=body, headers={**headers, "origin": "https://example.com"}).status_code == 403
        response = client.post("/api/v1/exams/accounts", json=body, headers=headers)
        assert response.status_code == 201
        item = response.json()
        assert not any(key.startswith("_") for key in item)
        assert client.post(f"/api/v1/exams/accounts/{item['id']}/actions", json={"action": "start"}, headers=headers).json()["status"] == "running"
        detail = client.get(f"/api/v1/exams/accounts/{item['id']}")
        assert detail.status_code == 200 and detail.json()["events"]
        assert detail.headers["cache-control"] == "no-store"
        assert client.get("/api/v1/exams/instruments").json()[0]["display_symbol"] == "MESZ6"
        assert client.post("/api/v1/exams/accounts", json={**body, "quantity": 100}, headers=headers).status_code == 422
