"""The sample page must never turn unavailable data into live quotes or zero volume."""
from copy import deepcopy
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from server import options_sample


def fixture():
    contract = {"conId": 1, "localSymbol": "TEST P7700", "lastTradeDateOrContractMonth": "20261026",
                "strike": 7700, "right": "P"}
    point = {"bar_start": "2026-10-16T20:30:00+00:00", "bar_end": "2026-10-16T21:00:00+00:00",
             "option_midpoint_close": 75, "underlying_midpoint_close": 7703, "iv": .13,
             "delta_future": -.4, "gamma_future": .002, "vega_per_1pct": 4, "theta_per_calendar_day": -3}
    es = {"contracts": [{"contract": contract}, {"contract": dict(contract, conId=2, localSymbol="TEST C7700", right="C")}],
          "model_estimates": [{"contract": contract, "points": [point]}], "interest_rate_annual": 0,
          "history": [{"contract": contract, "field": "MIDPOINT", "bars": [{"date": point["bar_start"], "volume": -1}]}]}
    vx = {"history": [{"contract": {"secType": "FUT", "symbol": "VIX", "localSymbol": "VXV6", "lastTradeDateOrContractMonth": "20261021"},
                       "whatToShow": "TRADES", "bars": [{"date": "2026-10-16T20:00:00+00:00", "close": 19.5}]}]}
    return es, vx


def test_missing_contract_volume_and_stale_vx_remain_missing():
    result = options_sample.build_sample(*fixture())
    assert result["expected_contracts"] == 2 and result["priced_contracts"] == 1
    assert result["trade_contracts"] == 0
    assert all(r["volume"] is None for r in result["rows"])
    assert next(r for r in result["rows"] if r["id"] == 2)["iv"] is None
    assert result["vx"][0]["price"] is None  # Do not silently carry forward stale VX prices.
    assert result["oi_pcr"] is result["full_chain_volume_pcr"] is result["full_chain_volume"] is None
    assert not result["automatic_collection"] and not result["current_quotes_available"]


@pytest.mark.parametrize("volume,expected", [(-1, None), (None, None), (0, 0), (23, 23), (float('nan'), None)])
def test_only_same_bar_valid_trade_volume_is_displayed(volume, expected):
    es, vx = fixture()
    trade = deepcopy(es["history"][0]); trade["field"] = "TRADES"; trade["bars"][0]["volume"] = volume
    es["history"].append(trade)
    result = options_sample.build_sample(es, vx)
    assert next(r for r in result["rows"] if r["id"] == 1)["volume"] == expected


def test_unavailable_time_is_rejected():
    with pytest.raises(ValueError, match="所选时间"):
        options_sample.build_sample(*fixture(), bar_start="2026-10-18T12:00:00+00:00")


def test_absent_report_returns_actionable_error_without_local_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(options_sample, "SAMPLE_DIR", tmp_path)
    app = FastAPI(); app.include_router(options_sample.router)
    with TestClient(app) as client:
        response = client.get('/api/v1/options/es/sample')
    assert response.status_code == 503
    assert "暂不可读" in response.json()["detail"]
    assert str(tmp_path) not in response.text
