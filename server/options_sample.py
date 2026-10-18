"""Read-only presentation of the ES feasibility run, never a live quote feed."""
from functools import lru_cache
import json
import math
from pathlib import Path

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/v1/options", tags=["options"])
SAMPLE_DIR = Path(__file__).resolve().parents[1] / "verification/options-probe"


def number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return value
    return None


def nonnegative(value):
    value = number(value)
    return value if value is not None and value >= 0 else None


def build_sample(es, vx, bar_start=None):
    estimates = es.get("model_estimates", [])
    times = sorted({p["bar_start"] for row in estimates for p in row.get("points", [])})
    if not times:
        raise ValueError("期权样本中没有可展示的计算结果。")
    selected = bar_start or times[-1]
    if selected not in times:
        raise ValueError("所选时间不在本地期权样本内。")
    points = {row["contract"]["conId"]: next((p for p in row.get("points", [])
              if p["bar_start"] == selected), None) for row in estimates}
    trades = {row["contract"]["conId"]: next((p for p in row.get("bars", [])
              if p["date"] == selected), None) for row in es.get("history", []) if row["field"] == "TRADES"}
    # Missing contracts remain visible; an absent trade bar is unknown, not zero.
    rows = []
    for item in es["contracts"]:
        c = item["contract"]
        p = points.get(c["conId"]) or {}
        trade = trades.get(c["conId"]) or {}
        rows.append({"id": c["conId"], "name": c["localSymbol"],
                     "expiry": c["lastTradeDateOrContractMonth"], "strike": c["strike"], "right": c["right"],
                     "midpoint": number(p.get("option_midpoint_close")), "iv": number(p.get("iv")),
                     "delta": number(p.get("delta_future")), "gamma": number(p.get("gamma_future")),
                     "vega": number(p.get("vega_per_1pct")), "theta": number(p.get("theta_per_calendar_day")),
                     "volume": nonnegative(trade.get("volume")), "oi": None, "bid": None, "ask": None})
    rows.sort(key=lambda r: (r["expiry"], r["strike"], r["right"]))
    chosen_points = [p for p in points.values() if p]
    underlying = number(chosen_points[0].get("underlying_midpoint_close"))
    expiries = sorted({r["expiry"] for r in rows})
    skew = [r for r in rows if underlying is not None and
            r["right"] == ("P" if r["strike"] < underlying else "C")]
    # The feasibility run uses a fixed 7700 strike, not an interpolated ATM curve.
    term = [r for r in skew if r["strike"] == 7700]
    vx_rows = []
    for h in vx.get("history", []):
        c = h["contract"]
        if c.get("symbol") not in {"VIX", "VX"} or c.get("secType") != "FUT" or h.get("whatToShow") != "TRADES":
            continue
        p = next((p for p in h.get("bars", []) if p["date"] == selected), {})
        vx_rows.append({"name": c["localSymbol"], "expiry": c["lastTradeDateOrContractMonth"],
                        "price": nonnegative(p.get("close"))})
    vx_rows.sort(key=lambda r: r["expiry"])
    return {"mode": "historical_sample", "target": "ES", "underlying": "ESZ6", "interval_minutes": 30,
            "collected_at": es.get("finished_at"), "bar_start": selected,
            "bar_end": chosen_points[0]["bar_end"], "available_times": times,
            "automatic_collection": False, "current_quotes_available": False,
            "model": "Black-76", "interest_rate": es["interest_rate_annual"],
            "rate_verified": False, "underlying_price": underlying, "expiries": expiries,
            "expected_contracts": len(rows), "priced_contracts": sum(r["iv"] is not None for r in rows),
            "trade_contracts": sum(r["volume"] is not None for r in rows),
            "rows": rows, "skew": skew, "term": term, "vx": vx_rows,
            "oi_pcr": None, "full_chain_volume": None, "full_chain_volume_pcr": None}


@lru_cache(maxsize=4)
def read_report(path, modified_ns):
    return json.loads(Path(path).read_text())


@router.get("/es/sample")
def es_sample(bar_start: str | None = None):
    try:
        reports = [SAMPLE_DIR / name for name in ("es-halfhour-sample-20261018.json", "es-history-20261018.json")]
        es, vx = [read_report(str(p), p.stat().st_mtime_ns) for p in reports]
        return build_sample(es, vx, bar_start)
    except (OSError, json.JSONDecodeError):
        raise HTTPException(503, "本地 ES 试验结果暂不可读，请检查样本文件。") from None
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
