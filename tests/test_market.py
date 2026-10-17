import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
import httpx

from server.app import create_app
from server.indicators import calculate, ema, rsi
from server.market import MarketService
from server.models import Candle, TIMEFRAMES, missing_ranges, parse_candle
from server.storage import Store


def bar(i=0, *, tf="15m", value=100.0, status="confirmed", source="rest", trades=10):
    start = i * TIMEFRAMES[tf]
    return Candle(tf, start, start + TIMEFRAMES[tf], value, value + 2, value - 2,
                  value, 1.0, trades, status, start + TIMEFRAMES[tf] + 3000, source=source)


def raw(b):
    return {"t": b.time, "T": b.end - 1, "s": "BTC", "i": b.timeframe,
            "o": str(b.open), "h": str(b.high), "l": str(b.low), "c": str(b.close),
            "v": str(b.volume), "n": b.trades}


def test_indicator_definitions_and_warmup():
    assert ema([1, 2, 3, 4, 5], 3) == [None, None, 2, 3, 4]
    assert rsi([100] * 20)[14:] == [50] * 6
    assert rsi(list(range(1, 31)))[-1] == 100
    assert rsi(list(range(30, 0, -1)))[-1] == 0
    values = calculate([bar(i, value=100 + i) for i in range(100)])
    assert values[24]["macd"] is None
    assert values[25]["macd"] == pytest.approx(7)
    assert values[32]["signal"] is None
    assert values[33]["signal"] == pytest.approx(7)
    assert values[-1]["histogram"] == pytest.approx(0)


def test_rsi_reference_sequence():
    # Wilder worked example: first 14 changes => RSI approximately 70.4641.
    prices = [44.34,44.09,44.15,43.61,44.33,44.83,45.10,45.42,45.84,46.08,45.89,46.03,45.61,46.28,46.28]
    assert rsi(prices)[-1] == pytest.approx(70.464135, abs=1e-5)


def test_gap_and_unconfirmed_bar_reset_indicators():
    bars = [bar(i, value=100 + i) for i in range(100)]
    interrupted = bars[:70] + bars[71:]
    assert calculate(interrupted)[70]["ema20"] is None
    bars[70] = replace(bars[70], status="forming")
    result = calculate(bars)
    assert result[70]["provisional"]
    assert result[71]["ema20"] is None


def test_only_post_boundary_rest_confirms():
    b = bar(10)
    assert parse_candle(raw(b), "15m", b.end + 5_000, source="ws").status == "forming"
    assert parse_candle(raw(b), "15m", b.end + 5_000, source="rest", requested_at=b.end - 1).status == "forming"
    assert parse_candle(raw(b), "15m", b.end + 5_000, source="rest", requested_at=b.end + 3_000).status == "confirmed"
    bad = {**raw(b), "h": "NaN"}
    with pytest.raises(ValueError):
        parse_candle(bad, "15m", b.end, source="ws")


def test_persistence_out_of_order_and_revision(tmp_path):
    path = tmp_path / "market.db"
    store = Store(path)
    b = bar(10)
    store.merge([b])
    assert store.merge([replace(b, close=101, status="forming", source="ws", trades=9)]) == 0
    assert store.bars("15m")[0].status == "confirmed"
    assert store.merge([replace(b, close=101, source="rest")]) == 1
    assert store.bars("15m")[0].revision == 2
    store.close()
    store = Store(path)
    assert store.bars("15m")[0].close == 101
    store.close()


def test_delayed_forming_rest_cannot_replace_new_ws(tmp_path):
    store = Store(tmp_path / "market.db")
    fresh = bar(10, source="ws", status="forming", trades=20)
    store.merge([fresh])
    assert store.merge([replace(fresh, trades=10, close=101, source="rest")]) == 0
    assert store.merge([replace(fresh, close=101)]) == 0
    assert store.bars("15m")[0].close == 100
    store.close()


def test_missing_tail_and_internal_gap():
    bars = [bar(0), bar(2)]
    gaps = missing_ranges(bars, "15m", 5 * TIMEFRAMES["15m"] + 3_000)
    assert gaps == [{"start": 900_000, "end": 1_800_000, "bars": 1},
                    {"start": 2_700_000, "end": 4_500_000, "bars": 2}]


def test_disconnect_repair_and_snapshot_quality(tmp_path, monkeypatch):
    at = 500 * TIMEFRAMES["4h"] + 10_000
    monkeypatch.setattr("server.market.now_ms", lambda: at)
    store = Store(tmp_path / "market.db")
    service = MarketService(store)
    for tf, step in TIMEFRAMES.items():
        latest = at // step
        # Persist a gap, stale tail and old forming candle, as after an interrupted session.
        store.merge([bar(i, tf=tf, status="forming" if i == latest - 3 else "confirmed")
                     for i in range(latest - 310, latest - 2) if i != latest - 5])
    assert service.overview()["timeframes"]["15m"]["quality"]["status"] == "stale"
    requests = []
    async def fake_info(body):
        requests.append(body)
        if body["type"] == "metaAndAssetCtxs":
            return [{"universe": [{"name": "BTC"}]}, [{"markPx": "100", "prevDayPx": "99", "dayNtlVlm": "1000", "funding": "0", "openInterest": "2"}]]
        req = body["req"]
        tf, step = req["interval"], TIMEFRAMES[req["interval"]]
        return [raw(bar(i, tf=tf)) for i in range(req["startTime"] // step, at // step + 1)]
    service._info = fake_info
    asyncio.run(service.reconcile())
    for tf in TIMEFRAMES:
        snapshot = service.chart(tf)
        assert snapshot["quality"]["status"] == "fresh"
        assert snapshot["quality"]["gaps"] == []
        assert snapshot["bars"][-1]["status"] == "forming"
        assert snapshot["bars"][-2]["status"] == "confirmed"
    counts = [len(store.bars(tf)) for tf in TIMEFRAMES]
    asyncio.run(service.reconcile())
    assert counts == [len(store.bars(tf)) for tf in TIMEFRAMES]
    # Simulate an upstream outage past freshness TTL, then recovery, without touching system networking.
    at += 90_000
    async def failed_info(body):
        raise httpx.ConnectError("simulated offline")
    service._info = failed_info
    asyncio.run(service.reconcile())
    assert service.overview()["quote_stale"]
    assert service.overview()["timeframes"]["15m"]["quality"]["status"] == "stale"
    assert counts == [len(store.bars(tf)) for tf in TIMEFRAMES]
    service._info = fake_info
    asyncio.run(service.reconcile())
    assert service.overview()["timeframes"]["15m"]["quality"]["status"] == "fresh"
    assert service.errors == {}
    store.close()


def test_api_empty_cache_validation_and_origin(tmp_path):
    with TestClient(create_app(tmp_path, live=False)) as client:
        overview = client.get("/api/v1/overview").json()
        assert overview["quote"] is None
        assert overview["transport"] == "offline"
        assert list(overview["timeframes"]) == list(TIMEFRAMES)
        for tf in TIMEFRAMES:
            response = client.get(f"/api/v1/chart/{tf}")
            assert response.status_code == 200
            assert response.json()["timeframe"] == tf
            assert response.json()["bars"] == []
        assert client.get("/api/v1/chart/invalid").status_code == 422
        assert client.post("/api/v1/refresh").status_code == 403
        assert client.post("/api/v1/refresh", headers={"X-Local-Market": "1", "Origin": "https://example.com"}).status_code == 403
        assert client.post("/api/v1/refresh", headers={"X-Local-Market": "1"}).status_code == 202
        assert client.get("/api/v1/health", headers={"Host": "example.com"}).status_code == 400


@pytest.mark.parametrize("tf", list(TIMEFRAMES))
def test_every_timeframe_boundary_and_indicator_warmup(tf):
    step = TIMEFRAMES[tf]
    candle = bar(100, tf=tf)
    # A candle remains provisional until a REST request is made beyond the grace period.
    assert parse_candle(raw(candle), tf, candle.end + 3000,
                        source="rest", requested_at=candle.end + 1999).status == "forming"
    assert parse_candle(raw(candle), tf, candle.end + 3000,
                        source="rest", requested_at=candle.end + 2000).status == "confirmed"
    with pytest.raises(ValueError, match="time bounds"):
        parse_candle({**raw(candle), "t": candle.time + 1000}, tf, candle.end, source="ws")
    assert missing_ranges([bar(100, tf=tf), bar(102, tf=tf)], tf, 103 * step + 3000) == [
        {"start": 101 * step, "end": 102 * step, "bars": 1}]
    indicators = calculate([bar(i, tf=tf, value=100+i) for i in range(80)])
    assert indicators[-1]["ema60"] is not None
    assert indicators[-1]["signal"] == pytest.approx(7)


def test_native_weekly_boundary_matches_live_hyperliquid_sample():
    # Public response observed 2026-10-11: weekly candle starts Thursday 00:00 UTC.
    start, end = 1791417600000, 1792022400000
    b = replace(bar(tf="1w"), time=start, end=end)
    parsed = parse_candle(raw(b), "1w", start + 86400000, source="ws")
    assert datetime.fromtimestamp(parsed.time / 1000, timezone.utc).weekday() == 3
    assert parsed.end == end
    assert missing_ranges([parsed], "1w", end + 3000) == []


def test_short_weekly_history_only_bootstraps_once(tmp_path, monkeypatch):
    at = 500 * TIMEFRAMES["1w"] + 10000
    monkeypatch.setattr("server.market.now_ms", lambda: at)
    store = Store(tmp_path / "market.db")
    service = MarketService(store)
    starts = []

    async def fake_info(body):
        if body["type"] == "metaAndAssetCtxs":
            return [{"universe": [{"name": "BTC"}]}, [{"markPx": "100", "prevDayPx": "99"}]]
        req = body["req"]
        tf, step = req["interval"], TIMEFRAMES[req["interval"]]
        latest = at // step
        if tf == "1w":
            starts.append(req["startTime"])
        return [raw(bar(i, tf=tf)) for i in range(max(latest-99, req["startTime"] // step), latest+1)]

    service._info = fake_info
    asyncio.run(service.reconcile())
    assert len(store.bars("1w")) == 100
    asyncio.run(service.reconcile())
    assert starts[1] == 498 * TIMEFRAMES["1w"]
    assert starts[1] > starts[0]
    assert service.chart("1w")["quality"]["status"] == "fresh"
    store.close()
