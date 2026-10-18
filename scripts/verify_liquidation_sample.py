#!/usr/bin/env python3
"""Sample LiqFlow liquidation prices against the official Hyperliquid API.

Why this is enough: ``clearinghouseState`` already returns ``liquidationPx``
for every open position, so there is no need to re-implement Hyperliquid's
maintenance-margin formula.  We compare the two numbers per (address, coin)
and report absolute/relative deviation plus the sampling window.

Exit code is 0 when the sample passes, 1 when it fails, 2 when it cannot run.

Usage:
    .venv/bin/python scripts/verify_liquidation_sample.py --samples 12
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.hyperliquid_info import HyperliquidInfo  # noqa: E402
from server.intel import utc_month  # noqa: E402
from server.intel_store import IntelStore, QuotaExceeded  # noqa: E402
from server.liqflow import LiqFlowClient, LiqFlowError  # noqa: E402

MONTHLY_LIMIT = 50_000
HARD_CAP = 45_000


def default_data_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/limen"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "limen"


def load_key(data_dir: Path) -> tuple[str, str]:
    env_key = (os.environ.get("LIQFLOW_API_KEY") or "").strip()
    if env_key:
        return env_key, "env"
    path = Path(os.environ.get("LIQFLOW_CONFIG") or (data_dir / "liqflow.json"))
    if not path.exists():
        raise SystemExit(f"找不到 {path}，请先放置 LiqFlow key（权限 600）")
    raw = json.loads(path.read_text("utf-8"))
    key = str(raw.get("api_key") or raw.get("key") or "").strip()
    if not key:
        raise SystemExit(f"{path} 缺少 api_key")
    return key, str(path)


def number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def sample(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir).expanduser() if args.data_dir else default_data_dir()
    key, source = load_key(data_dir)
    store = IntelStore(data_dir)
    client = LiqFlowClient(key, min_interval=3.0,
                           on_attempt=lambda path: store.quota_begin(
                               month=utc_month(), endpoint=path, limit=MONTHLY_LIMIT,
                               hard_cap=HARD_CAP))
    hl = HyperliquidInfo()
    try:
        try:
            payload = await client.get("/api/positions/top", {"limit": args.positions_limit})
        except QuotaExceeded as exc:
            print(f"本地额度账本已触顶：{exc}", file=sys.stderr)
            return 2
        except LiqFlowError as exc:
            print(f"LiqFlow 调用失败：{exc.as_dict()}", file=sys.stderr)
            return 2
        finally:
            rate = client.last_rate_limit
            if rate.remaining is not None:
                store.quota_record(month=utc_month(), endpoint="/api/positions/top",
                                   status=200, provider_limit=rate.limit,
                                   provider_remaining=rate.remaining)

        rows = payload.get("positions") or []
        if not rows:
            print("持仓榜为空，无法抽样", file=sys.stderr)
            return 2
        by_address: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            address = str(row.get("address") or "").lower()
            if address.startswith("0x") and len(address) == 42:
                by_address.setdefault(address, []).append(row)
        if not by_address:
            print("持仓榜没有有效地址", file=sys.stderr)
            return 2

        rng = random.Random(args.seed)
        addresses = list(by_address)
        picked = rng.sample(addresses, min(args.samples, len(addresses)))
        print(f"抽样 {len(picked)} 地址（候选 {len(addresses)}，seed={args.seed}）")

        comparisons: list[dict[str, Any]] = []
        size_checks: list[float] = []
        failures: list[str] = []
        started = time.time()
        for index, address in enumerate(picked, 1):
            try:
                state = await hl.clearinghouse_state(address)
            except Exception as exc:  # noqa: BLE001 - report, keep sampling
                failures.append(f"{address}: {type(exc).__name__}")
                continue
            official = {}
            for entry in state.get("assetPositions") or []:
                position = entry.get("position") or {}
                coin = str(position.get("coin") or "")
                if coin:
                    official[coin] = position
            for row in by_address[address]:
                coin = str(row.get("coin") or "")
                if args.symbol and coin.upper() != args.symbol.upper():
                    continue
                if coin not in official:
                    continue
                liqflow = number(row.get("liq_price"))
                real = number(official[coin].get("liquidationPx"))
                if liqflow is None or real is None or real == 0:
                    continue
                absolute = abs(liqflow - real)
                pct = absolute / abs(real) * 100
                comparisons.append({
                    "address": address, "coin": coin, "liqflow": liqflow,
                    "official": real, "abs_diff": absolute, "pct": pct,
                    "leverage": row.get("leverage"), "size": row.get("size"),
                })
                if row.get("size") is not None:
                    official_size = number(official[coin].get("szi"))
                    if official_size:
                        size_checks.append(abs(float(row["size"]) - official_size)
                                           / abs(official_size) * 100)
            if index < len(picked):
                await asyncio.sleep(args.pause)

        if not comparisons:
            print("没有可比对的仓位（可能标的过滤过严或地址无持仓）", file=sys.stderr)
            return 2

        pcts = [item["pct"] for item in comparisons]
        within = [item for item in comparisons if item["pct"] <= args.threshold]
        pass_ratio = len(within) / len(comparisons)
        passed = len(comparisons) >= args.min_samples and pass_ratio >= args.pass_ratio
        summary = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "liqflow_snapshot": payload.get("snapshot_time"),
            "source": source,
            "candidates": len(addresses),
            "sampled_addresses": len(picked),
            "comparisons": len(comparisons),
            "threshold_pct": args.threshold,
            "within_threshold": len(within),
            "pass_ratio": round(pass_ratio, 4),
            "max_pct": round(max(pcts), 4),
            "median_pct": round(statistics.median(pcts), 4),
            "p90_pct": round(sorted(pcts)[max(0, int(len(pcts) * 0.9) - 1)], 4),
            "mean_abs_diff": round(statistics.fmean(item["abs_diff"] for item in comparisons), 6),
            "size_matches": len(size_checks),
            "size_max_pct": round(max(size_checks), 4) if size_checks else None,
            "passed": passed,
            "failures": failures,
            "elapsed_seconds": round(time.time() - started, 1),
            "samples": comparisons,
        }

        reports = data_dir / "intel" / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        report_path = reports / f"liquidation-sample-{stamp}.json"
        report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), "utf-8")
        os.chmod(report_path, 0o600)

        for coin in sorted({item["coin"] for item in comparisons}):
            subset = [item for item in comparisons if item["coin"] == coin]
            if len(subset) < 3:
                continue
            coin_ok = all(item["pct"] <= args.threshold for item in subset)
            store.set_coverage(
                coin, status="sampled_pass" if coin_ok else "sampled_fail",
                samples=len(subset), within_threshold=sum(item["pct"] <= args.threshold for item in subset),
                max_abs_pct=max(item["pct"] for item in subset),
                report_path=str(report_path),
                note=f"LiqFlow vs 官方 clearinghouseState，阈值 {args.threshold}%")
        store.set_coverage(
            "__GLOBAL__", status="sampled_pass" if passed else "sampled_fail",
            samples=len(comparisons), within_threshold=len(within),
            max_abs_pct=max(pcts), report_path=str(report_path),
            note=f"top{args.positions_limit} 抽样：{len(picked)} 地址 / {len(comparisons)} 仓位")

        print(f"\n{'coin':<8}{'LiqFlow':>14}{'official':>14}{'diff%':>9}  address")
        for item in comparisons[:args.print_rows]:
            print(f"{item['coin']:<8}{item['liqflow']:>14.6f}{item['official']:>14.6f}"
                  f"{item['pct']:>9.4f}  {item['address'][:10]}…")
        print(f"\n比对 {len(comparisons)} 仓位｜阈值内 {len(within)}（{pass_ratio:.1%}）"
              f"｜中位偏差 {summary['median_pct']:.4f}%｜最大 {summary['max_pct']:.4f}%")
        if size_checks:
            print(f"数量核对 {len(size_checks)} 条｜最大偏差 {summary['size_max_pct']:.4f}%")
        print(f"结论：{'抽样通过' if passed else '未通过'}｜报告 {report_path}")
        if failures:
            print(f"官方接口失败 {len(failures)} 个地址（已跳过）")
        return 0 if passed else 1
    finally:
        await client.aclose()
        await hl.aclose()
        store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=12)
    parser.add_argument("--positions-limit", type=int, default=25)
    parser.add_argument("--threshold", type=float, default=0.5,
                        help="允许的相对偏差百分比（默认 0.5%%）")
    parser.add_argument("--pass-ratio", type=float, default=0.9,
                        help="达标比例（默认 90%%）")
    parser.add_argument("--min-samples", type=int, default=10)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--pause", type=float, default=0.3,
                        help="官方接口调用间隔（秒）")
    parser.add_argument("--print-rows", type=int, default=15)
    args = parser.parse_args()
    args.samples = max(1, min(20, args.samples))
    return asyncio.run(sample(args))


if __name__ == "__main__":
    raise SystemExit(main())
