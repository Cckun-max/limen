import asyncio
from dataclasses import replace
import json
import sqlite3

from fastapi import HTTPException
from fastapi.testclient import TestClient
import httpx
import pytest

from server.ai import snapshot_market, model_snapshot
from server.app import create_app
from server.catalog import MarketHub, parse_catalog
from server.market import MarketService
from server.models import Candle, TIMEFRAMES, now_ms, parse_candle
from server.storage import Store

HEADERS = {"X-Local-Market": "1"}


def catalog():
    return [{"universe": [{"name": "BTC", "szDecimals": 5}, {"name": "ETH", "szDecimals": 4},
                           {"name": "DOGE", "szDecimals": 0}, {"name": "OLD", "isDelisted": True}]},
            [{"markPx": "60000", "prevDayPx": "50000", "openInterest": "2", "dayNtlVlm": "1000"},
             {"markPx": "3000", "prevDayPx": "3100", "openInterest": "100", "funding": "0.00001"},
             {"markPx": "0.08", "prevDayPx": "0.1"}, {"markPx": "1", "prevDayPx": "1"}]]


def test_catalog_alignment_units_missing_values_and_invalid_payload():
    rows = parse_catalog(catalog(), 123)
    assert rows[0]["change_pct"] == pytest.approx(20)
    assert rows[1]["oi_base"] == 100 and rows[1]["oi_usd"] == 300000
    assert rows[1]["volume_usd"] is None
    assert rows[2]["mark"] == .08 and rows[3]["delisted"]
    bad = catalog()
    bad[1].pop()
    with pytest.raises(ValueError):
        parse_catalog(bad, 123)
    bad = catalog()
    bad[1][0]["markPx"] = "NaN"
    assert parse_catalog(bad, 123)[0]["mark"] is None


def test_v1_database_migration_keeps_cache_revision_metadata_and_ai(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.executescript("""CREATE TABLE candles (timeframe TEXT, time INTEGER, end INTEGER,
            open REAL, high REAL, low REAL, close REAL, volume REAL, trades INTEGER,
            status TEXT, received_at INTEGER, revision INTEGER, source TEXT,
            PRIMARY KEY(timeframe,time));
            INSERT INTO candles VALUES ('1m',0,60000,100,101,99,100,2,3,'confirmed',63000,7,'rest');
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO metadata VALUES ('test','{"ok":true}');
            CREATE TABLE ai_snapshots (id TEXT PRIMARY KEY, created_at INTEGER, payload TEXT);
            INSERT INTO ai_snapshots VALUES ('saved',123,'{"symbol":"BTC"}');
            PRAGMA user_version=1;""")
    store = Store(path)
    btc = store.bars("1m")[0]
    assert btc.symbol == "BTC" and btc.revision == 7
    assert store.get("test") == {"ok": True}
    assert store.db.execute("SELECT payload FROM ai_snapshots").fetchone()[0] == '{"symbol":"BTC"}'
    store.merge([replace(btc, symbol="ETH", close=99, revision=1)])
    assert store.bars("1m", symbol="ETH")[0].close == 99
    assert store.bars("1m")[0].close == 100
    store.close()
    store = Store(path)
    assert store.bars("1m", symbol="ETH")[0].symbol == "ETH"
    store.close()


def test_cross_symbol_candle_rejected_and_snapshot_keeps_symbol(tmp_path):
    store = Store(tmp_path / "market.db")
    market = MarketService(store, "ETH")
    at = now_ms()
    for tf, step in TIMEFRAMES.items():
        end = at // step * step
        store.merge([Candle(tf, end - step * (100 - i), end - step * (99 - i), 3000, 3002, 2998, 3000,
                            2, 10, "confirmed", at, symbol="ETH") for i in range(100)])
        market.last_rest[tf] = at
        market.needs_reconcile[tf] = False
    market._update_quote(catalog()[1][1], "rest")
    snap = snapshot_market(market, "1h")
    assert snap["ready"] and snap["symbol"] == "ETH"
    assert snap["instrument_id"] == "hyperliquid:perp:ETH"
    assert snap["units"]["volume"] == "ETH" and "BTC" not in json.dumps(snap)
    assert model_snapshot(snap)["symbol"] == "ETH"
    assert store.bars("1h") == []
    with pytest.raises(ValueError, match="instrument"):
        parse_candle({"s": "BTC", "i": "1m", "t": 0}, "1m", at, source="ws", symbol="ETH")
    store.close()


def test_watchlists_persist_deduplicate_and_delete_group_without_losing_items(tmp_path):
    with TestClient(create_app(tmp_path, live=False)) as client:
        client.app.state.hub.accept(catalog())
        assert client.post('/api/v1/watchlists', json={"name": "观察"}).status_code == 403
        assert client.post('/api/v1/watchlists', json={"name": "观察"}, headers={**HEADERS, "Origin": "https://evil.example"}).status_code == 403
        for name in ["", " " * 3, "X" * 31]:
            assert client.post('/api/v1/watchlists', json={"name": name}, headers=HEADERS).status_code == 422
        groups = client.post('/api/v1/watchlists', json={"name": "观察"}, headers=HEADERS).json()
        group = next(g for g in groups if g["name"] == "观察")
        base = f'/api/v1/watchlists/{group["id"]}'
        assert client.post('/api/v1/watchlists', json={"name": "观察"}, headers=HEADERS).status_code == 409
        for _ in range(2):
            assert client.put(base + '/items/ETH', headers=HEADERS).status_code == 200
        assert client.put(base + '/items/OLD', headers=HEADERS).status_code == 422
        assert client.put(base + '/items/FAKE', headers=HEADERS).status_code == 422
        groups = client.patch(base, json={"name": "重点"}, headers=HEADERS).json()
        assert next(g for g in groups if g["id"] == group["id"])["symbols"] == ["ETH"]
    with TestClient(create_app(tmp_path, live=False)) as client:
        groups = client.get('/api/v1/watchlists').json()
        assert next(g for g in groups if g["name"] == "重点")["symbols"] == ["ETH"]
        groups = client.delete(base, headers=HEADERS).json()
        assert groups[0]["symbols"] == ["ETH"]
        assert client.delete('/api/v1/watchlists/default', headers=HEADERS).status_code == 422
        assert client.delete('/api/v1/watchlists/missing/items/ETH', headers=HEADERS).status_code == 404
        assert client.delete('/api/v1/watchlists/default/items/ETH', headers=HEADERS).json()[0]["symbols"] == []


def test_symbol_api_catalog_cache_and_delisted_survival(tmp_path):
    with TestClient(create_app(tmp_path, live=False)) as client:
        hub = client.app.state.hub
        hub.accept(catalog())
        for symbol in ('BTC', 'ETH', 'DOGE'):
            result = client.get('/api/v1/chart/1h', params={"symbol": symbol}).json()
            assert result["symbol"] == symbol and result["bars"] == []
            snap = client.post('/api/v1/ai/snapshots', json={"symbol": symbol, "timeframe": "1h"}, headers=HEADERS).json()
            assert snap["symbol"] == symbol
        assert client.get('/api/v1/overview?symbol=FAKE').status_code == 422
        old = catalog()
        old[0]["universe"].pop(1)
        old[1].pop(1)
        hub.accept(old)
        assert next(r for r in hub.public()["items"] if r["symbol"] == "ETH")["delisted"]
    with TestClient(create_app(tmp_path, live=False)) as client:
        assert len(client.get('/api/v1/markets').json()["items"]) == 4


def test_hub_reuses_same_feed_and_does_not_evict_active_clients(tmp_path):
    async def scenario():
        store = Store(tmp_path / "market.db")
        hub = MarketHub(store, live=False)
        hub.MAX_FEEDS = 2
        hub.accept(catalog())
        btc = await hub.acquire('BTC')
        assert await hub.get('BTC') is btc
        eth = await hub.acquire('ETH')
        with pytest.raises(HTTPException) as error:
            await hub.get('DOGE')
        assert error.value.status_code == 429
        hub.release('BTC')
        await hub.get('DOGE')
        assert 'BTC' not in hub.feeds and hub.feeds['ETH'] is eth
        hub.release('ETH')
        await hub.stop()
        store.close()
    asyncio.run(scenario())


def test_catalog_refresh_bypasses_cached_response(tmp_path):
    async def scenario():
        store = Store(tmp_path / "market.db")
        hub = MarketHub(store, live=False)
        hub.accept(catalog())
        calls = []
        def respond(request):
            calls.append(request)
            updated = catalog()
            updated[1][0]["markPx"] = "70000"
            return httpx.Response(200, json=updated)
        hub.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        assert (await hub.info({"type": "metaAndAssetCtxs"}))[1][0]["markPx"] == "60000"
        hub.accept(await hub.info({"type": "metaAndAssetCtxs"}, fresh=True))
        assert hub.public()["items"][0]["mark"] == 70000 and len(calls) == 1
        await hub.stop()
        store.close()
    asyncio.run(scenario())


def test_cached_quote_cannot_replace_newer_ws_or_gain_a_new_timestamp(tmp_path):
    store = Store(tmp_path / "market.db")
    market = MarketService(store, 'ETH')
    context = catalog()[1][1]
    market._update_quote(context, 'rest', 1000)
    assert market.quote['received_at'] == 1000
    market._update_quote({**context, 'markPx': '3001'}, 'ws', 2000)
    market._update_quote(context, 'rest', 1000)
    assert market.quote['mark'] == 3001 and market.quote['received_at'] == 2000
    store.close()
