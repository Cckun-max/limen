import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from server.app import create_app
from server.hyperliquid_info import HyperliquidError
from server.intel import IntelService, classify_fill, load_key_config, utc_month
from server.intel_store import IntelStore, QuotaExceeded
from server.liqflow import LiqFlowClient, LiqFlowError, RateLimit, parse_snapshot_time

ADDRESS = "0x" + "a" * 40


def run(coro):
    return asyncio.run(coro)


def test_snapshot_time_parsing_is_always_utc():
    zulu = parse_snapshot_time("2026-10-17T16:07:07.260959Z")
    naive = parse_snapshot_time("2026-10-17T16:07:07.260959")
    offset = parse_snapshot_time("2026-10-18T00:07:07.260959+08:00")
    assert zulu == naive == offset
    assert zulu.tzinfo == timezone.utc
    assert parse_snapshot_time(None) is None
    assert parse_snapshot_time("not-a-date") is None


def test_client_sends_bearer_and_parses_rate_headers():
    def handler(request):
        assert request.headers["authorization"] == "Bearer secret"
        return httpx.Response(200, json={"wallet_count": 1},
                              headers={"x-ratelimit-limit": "50000",
                                       "x-ratelimit-remaining": "49999",
                                       "x-ratelimit-period": "month"})

    async def scenario():
        client = LiqFlowClient("secret", min_interval=0,
                               transport=httpx.MockTransport(handler))
        try:
            assert await client.get("/api/stats") == {"wallet_count": 1}
            assert client.last_rate_limit == RateLimit(50000, 49999, "month",
                                                       client.last_rate_limit.observed_at)
        finally:
            await client.aclose()

    run(scenario())


def test_client_does_not_retry_auth_failures():
    attempts = []

    def handler(request):
        attempts.append(request.url.path)
        return httpx.Response(401, json={"error": "unauthorized"})

    async def scenario():
        client = LiqFlowClient("bad", min_interval=0,
                               transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(LiqFlowError) as caught:
                await client.get("/api/stats")
            assert caught.value.kind == "auth"
        finally:
            await client.aclose()

    run(scenario())
    assert len(attempts) == 1


def test_client_retries_timeout_once():
    attempts = []

    def handler(request):
        attempts.append(request.url.path)
        if len(attempts) == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json={"ok": True})

    async def scenario():
        client = LiqFlowClient("secret", min_interval=0,
                               transport=httpx.MockTransport(handler))
        try:
            assert await client.get("/api/stats") == {"ok": True}
        finally:
            await client.aclose()

    run(scenario())
    assert len(attempts) == 2


def test_client_surfaces_retry_after_on_429():
    def handler(request):
        return httpx.Response(429, headers={"retry-after": "120"}, json={})

    async def scenario():
        client = LiqFlowClient("secret", min_interval=0,
                               transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(LiqFlowError) as caught:
                await client.get("/api/stats")
            assert caught.value.kind == "rate_limit"
            assert caught.value.retry_after == 120
        finally:
            await client.aclose()

    run(scenario())


def test_quota_ledger_warns_then_blocks(tmp_path):
    store = IntelStore(tmp_path, cap_bytes=10_000)
    try:
        month = utc_month()
        progress = [store.quota_begin(month=month, endpoint="/x", limit=10, hard_cap=8)
                    for _ in range(8)]
        assert progress[-1]["used"] == 8
        assert progress[7]["warning"] is True
        with pytest.raises(QuotaExceeded):
            store.quota_begin(month=month, endpoint="/x", limit=10, hard_cap=8)
        state = store.quota_state(month=month, limit=10, hard_cap=8)
        assert state["used"] == 8 and state["exhausted"] is True
        store.quota_record(month=month, endpoint="/x", status=200,
                           provider_limit=50_000, provider_remaining=49_990)
        assert store.quota_state(month=month, limit=50_000,
                                 hard_cap=45_000)["used"] == 10
    finally:
        store.close()


def test_snapshots_roundtrip_and_prune_keeps_latest(tmp_path):
    store = IntelStore(tmp_path, cap_bytes=10_000)
    try:
        payload = {"snapshot_time": "2026-10-17T16:07:07Z",
                   "levels": [{"coin": "ETH", "liq_price": 100, "long_notional": 5}]}
        for i in range(12):
            store.save_snapshot("levels", payload, snapshot_time=payload["snapshot_time"],
                                fetched_at=1_700_000_000 + i * 600)
        latest = store.latest_snapshot("levels")
        assert latest["payload"] == payload
        before = store.storage_usage()
        assert store.prune(cap_bytes=max(2000, before // 2))["removed_files"] > 0
        assert store.latest_snapshot("levels")["payload"] == payload
    finally:
        store.close()


def test_event_classification_covers_all_transitions():
    def fill(side, size, start):
        return {"coin": "ETH", "sz": str(size), "px": "100", "startPosition": str(start),
                "side": side, "time": "1700000000", "tid": 1, "closedPnl": "1.5"}

    opened = classify_fill(fill("B", 2, 0))
    assert (opened["event"], opened["direction"]) == ("open", "long")
    added = classify_fill(fill("B", 2, 5))
    assert (added["event"], added["direction"]) == ("add", "long")
    reduced = classify_fill(fill("A", 2, 5))
    assert (reduced["event"], reduced["direction"]) == ("reduce", "long")
    closed = classify_fill(fill("A", 5, 5))
    assert (closed["event"], closed["direction"]) == ("close", "long")
    flipped = classify_fill(fill("B", 7, -5))
    assert (flipped["event"], flipped["direction"]) == ("flip", "long")
    assert flipped["end_position"] == 2
    assert classify_fill({"side": "B", "sz": "0"}) is None


class FakeClient:
    def __init__(self, payloads=None, error=None):
        self.payloads = payloads or {}
        self.error = error
        self.calls = []
        self.last_rate_limit = RateLimit(50_000, 49_990, "month", 0)
        self.on_attempt = None
        self.closed = False

    async def get(self, path, params=None):
        if self.on_attempt:
            self.on_attempt(path)
        if self.error:
            raise self.error
        self.calls.append((path, params))
        return self.payloads[path]

    async def aclose(self):
        self.closed = True


class FakeHL:
    def __init__(self, fills=None, error=None):
        self.fills = fills or []
        self.error = error
        self.calls = []

    async def user_fills(self, address, start_time=None):
        if self.error:
            raise self.error
        self.calls.append((address, start_time))
        return list(self.fills)


def make_payloads():
    levels = {"snapshot_time": "2026-10-17T16:07:07.260959Z",
              "levels": [
                  {"coin": "ETH", "liq_price": 100.0, "long_notional": 5.0},
                  {"coin": "ETH", "liq_price": 110.0, "short_notional": 7.0},
                  {"coin": "ETH", "liq_price": 110.0, "short_notional": 1.0},
                  {"coin": "BTC", "liq_price": 50000.0, "long_notional": 1.0},
              ]}
    stats = {"wallet_count": 10, "position_count": 5, "total_coins": 2,
             "last_snapshot_time": "2026-10-17T16:07:07.260959"}
    positions = {"limit": 25, "side": None, "positions": [
        {"address": ADDRESS, "coin": "ETH", "cohort_label": "Whale",
         "size": -10, "leverage": 5, "liq_price": 110.0, "entry_px": 90,
         "unrealized_pnl": -2, "account_value": 1000, "total_pnl": 5}]}
    return {"/api/aggregate/liq-levels": levels, "/api/stats": stats,
            "/api/positions/top": positions}


def make_service(tmp_path, payloads=None, *, hard_cap=45_000, hl=None):
    client = FakeClient(payloads if payloads is not None else make_payloads())
    service = IntelService(tmp_path, live=False, api_key="test", client=client,
                           hl_client=hl or FakeHL(), hard_cap=hard_cap)
    client.on_attempt = service._quota_attempt
    return service, client


def test_collect_once_caches_and_backs_off(tmp_path):
    service, client = make_service(tmp_path)

    async def scenario():
        first = await service.collect_once(("levels", "stats", "positions"))
        assert first["ok"] and all(item["ok"] for item in first["results"].values())
        assert service._levels_interval == 300
        second = await service.collect_once(("levels",))
        assert second["results"]["levels"]["ok"]
        assert service._levels_interval == 600
        await service.collect_once(("levels",))
        assert service._levels_interval == 600  # normal mode never exceeds 10 minutes
        await service.stop()

    run(scenario())
    assert len(client.calls) == 5


def test_collect_writes_readable_overview(tmp_path):
    service = IntelService(tmp_path, live=False, api_key="test",
                           client=FakeClient(make_payloads()), hl_client=FakeHL())
    payloads = make_payloads()

    async def scenario():
        await service.collect_once(("levels", "positions"))
        view = service.overview("ETH")
        assert view["available"] is True
        assert view["snapshot_time"].endswith("Z")
        assert view["levels"] == [
            {"price": 100.0, "long": 5.0, "short": None},
            {"price": 110.0, "long": None, "short": 8.0},
        ]
        assert view["coverage"]["status"] == "unverified"
        assert view["positions"][0]["tracked"] is True
        assert view["status"]["watch_count"] == 1
        unknown = service.overview("DOGE")
        assert unknown["available"] is False
        assert "未覆盖" in unknown["reason"]
        ibkr = service.overview("ibkr:123")
        assert ibkr["available"] is False and "IBKR" in ibkr["reason"]
        await service.stop()

    run(scenario())
    # A concurrent store instance must still see the same rows (no in-memory-only state).
    store = IntelStore(tmp_path, cap_bytes=10_000)
    try:
        assert store.latest_snapshot("levels")["payload"]["levels"]
    finally:
        store.close()


def test_missing_snapshot_never_fabricates_buckets(tmp_path):
    service = IntelService(tmp_path, live=False, api_key="test",
                           client=FakeClient({}), hl_client=FakeHL())

    async def scenario():
        view = service.overview("ETH")
        assert view["available"] is False
        assert view["levels"] == []
        assert "尚无" in view["reason"]
        await service.stop()

    run(scenario())


def test_auth_failure_pauses_collector(tmp_path):
    service = IntelService(tmp_path, live=False, api_key="test",
                           client=FakeClient(error=LiqFlowError("auth", "bad key", status=401)),
                           hl_client=FakeHL())

    async def scenario():
        result = await service.collect_once(("levels",))
        assert result["results"]["levels"]["error"] == "auth"
        status = service.status()
        assert status["mode"] == "paused"
        assert "unauthorized" in status["reasons"]
        await service.stop()

    run(scenario())


def test_quota_cap_pauses_before_http(tmp_path):
    payloads = make_payloads()
    client = FakeClient(payloads)
    service = IntelService(tmp_path, live=False, api_key="test", client=client,
                           hl_client=FakeHL(), hard_cap=1)
    client.on_attempt = service._quota_attempt

    async def scenario():
        result = await service.collect_once(("levels", "stats"))
        assert result["results"]["levels"]["ok"]
        assert result["results"]["stats"]["error"] == "quota_exhausted"
        assert service.status()["mode"] == "paused"
        await service.stop()

    run(scenario())
    assert len(client.calls) == 1


def test_fills_are_classified_and_deduplicated(tmp_path):
    now = 1_700_000_000_000
    fills = [
        {"coin": "ETH", "sz": "2", "px": "100", "startPosition": "0", "side": "B",
         "time": now, "tid": 11, "closedPnl": "0", "dir": "Open Long"},
        {"coin": "ETH", "sz": "1", "px": "101", "startPosition": "1", "side": "A",
         "time": now + 1000, "tid": 12, "closedPnl": "1", "dir": "Close Long"},
    ]
    hl = FakeHL(fills=fills)
    service = IntelService(tmp_path, live=False, api_key="test",
                           client=FakeClient(make_payloads()), hl_client=hl)
    service.store.watch_add(ADDRESS, label="Whale", source="manual")

    async def scenario():
        first = await service._poll_fills()
        assert first["inserted"] == 2
        assert service.store.events(10)[0]["event"] in {"open", "close"}
        second = await service._poll_fills()
        assert second["inserted"] == 0
        assert hl.calls[1][1] == now + 1001
        await service.stop()

    run(scenario())


def test_watchlist_validation_and_limit(tmp_path):
    service = IntelService(tmp_path, live=False, api_key="test",
                           client=FakeClient({}), hl_client=FakeHL())
    with pytest.raises(ValueError):
        service.add_watch("not-an-address")
    assert service.add_watch(ADDRESS)["added"] is True
    assert service.remove_watch(ADDRESS)["removed"] is True


def test_key_file_is_tightened_to_600(tmp_path):
    path = tmp_path / "liqflow.json"
    path.write_text(json.dumps({"api_key": "secret"}))
    os.chmod(path, 0o644)
    config = load_key_config(tmp_path)
    assert config.api_key == "secret"
    assert config.source == "file"
    assert (path.stat().st_mode & 0o777) == 0o600


def test_intel_api_reads_cache_and_guards_mutations(tmp_path):
    app = create_app(tmp_path, live=False)
    with TestClient(app) as client:
        status = client.get("/api/v1/intel/status").json()
        assert status["configured"] is False
        assert status["mode"] == "unconfigured"

        overview = client.get("/api/v1/intel/overview?symbol=ETH").json()
        assert overview["available"] is False and overview["levels"] == []

        assert client.post("/api/v1/intel/watchlist", json={"address": ADDRESS}).status_code == 403
        headers = {"X-Local-Market": "1"}
        assert client.post("/api/v1/intel/watchlist", headers=headers,
                           json={"address": "bad"}).status_code == 422
        added = client.post("/api/v1/intel/watchlist", headers=headers,
                            json={"address": ADDRESS, "label": "Whale"})
        assert added.status_code == 200 and added.json()["count"] == 1
        assert client.get("/api/v1/intel/watchlist").json()["addresses"][0]["address"] == ADDRESS.lower()
        assert client.delete(f"/api/v1/intel/watchlist/{ADDRESS}",
                             headers=headers).json()["removed"] is True
        assert client.post("/api/v1/intel/refresh", headers=headers).status_code == 409
        assert client.get("/api/v1/intel/events").json()["events"] == []


def test_rate_limit_pauses_with_retry_after(tmp_path):
    service = IntelService(tmp_path, live=False, api_key="test",
                           client=FakeClient(error=LiqFlowError("rate_limit", "429",
                                                                status=429, retry_after=60)),
                           hl_client=FakeHL())

    async def scenario():
        await service.collect_once(("levels",))
        status = service.status()
        assert status["mode"] == "paused"
        assert "rate_limited" in status["reasons"]
        assert status["retry_at"] > 0
        await service.stop()

    run(scenario())


def test_network_failures_degrade_after_three_rounds(tmp_path):
    service = IntelService(tmp_path, live=False, api_key="test",
                           client=FakeClient(error=LiqFlowError("network", "offline")),
                           hl_client=FakeHL())

    async def scenario():
        for _ in range(2):
            await service.collect_once(("levels",))
        assert service.status()["mode"] == "normal"
        await service.collect_once(("levels",))
        status = service.status()
        assert status["mode"] == "degraded"
        assert "upstream_error" in status["reasons"]
        await service.stop()

    run(scenario())


def test_projected_monthly_calls_stays_under_cap(tmp_path):
    service = IntelService(tmp_path, live=False, api_key="test",
                           client=FakeClient({}), hl_client=FakeHL())
    projected = service.projected_monthly_calls()
    assert projected == 21_600
    assert projected < service.hard_cap < service.monthly_limit
    run(service.stop())


def test_quota_resets_on_utc_month_boundary(tmp_path):
    store = IntelStore(tmp_path, cap_bytes=10_000)
    try:
        for _ in range(3):
            store.quota_begin(month="2026-10", endpoint="/x", limit=10, hard_cap=8)
        first = store.quota_begin(month="2026-11", endpoint="/x", limit=10, hard_cap=8)
        assert first["used"] == 1
        assert store.quota_state(month="2026-11", limit=10, hard_cap=8)["used"] == 1
        assert store.quota_state(month="2026-10", limit=10, hard_cap=8)["used"] == 3
    finally:
        store.close()


def test_downsample_keeps_newest_per_hour(tmp_path):
    store = IntelStore(tmp_path, cap_bytes=10_000_000)
    try:
        base = int(time.time()) - 72 * 3600
        base -= base % 3600
        for offset in (0, 60, 120, 180):
            store.save_snapshot("levels", {"n": offset}, snapshot_time=None,
                                fetched_at=base + offset)
        files = sorted((tmp_path / "intel" / "raw" / "levels").rglob("*.json.gz"))
        assert len(files) == 4
        for index, path in enumerate(files):
            stamp = base + index * 60
            os.utime(path, (stamp, stamp))
        removed = store.downsample(older_than_hours=48)
        remaining = sorted((tmp_path / "intel" / "raw" / "levels").rglob("*.json.gz"))
        assert removed > 0
        assert len(remaining) == 1
        assert store.latest_snapshot("levels")["payload"] == {"n": 180}
    finally:
        store.close()


def test_global_coverage_is_used_as_fallback(tmp_path):
    store = IntelStore(tmp_path, cap_bytes=10_000)
    try:
        assert store.coverage_effective("ETH")["scope"] == "none"
        store.set_coverage("__GLOBAL__", status="sampled_pass", samples=19,
                           within_threshold=18, max_abs_pct=0.5089, report_path="/r.json")
        effective = store.coverage_effective("ETH")
        assert effective["status"] == "sampled_pass"
        assert effective["scope"] == "global_top25"
        store.set_coverage("ETH", status="sampled_pass", samples=3,
                           within_threshold=3, max_abs_pct=0.1, report_path="/e.json")
        assert store.coverage_effective("ETH")["scope"] == "coin"
    finally:
        store.close()
