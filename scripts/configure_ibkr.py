#!/usr/bin/env python3
"""Inspect or update the developer's local IBKR data connection.

With no setting flags this is read-only, including when no database exists.
Changes take effect when the market service is restarted. Nothing is exposed
through the web app, and this command never connects to TWS / IB Gateway.
"""

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pydantic import ValidationError

from server.ibkr import ConnectionSettings
from server.storage import Store


def data_directory(explicit=None):
    configured = explicit if explicit is not None else os.environ.get("MARKET_DATA_DIR")
    if configured is not None:
        return Path(configured).expanduser()
    base = (Path.home() / "Library/Application Support" if sys.platform == "darwin"
            else Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")))
    folder, legacy = base / "tide", base / "LocalMarket"
    # The service migrates this folder on startup. Merely inspecting settings
    # must not rename it or create a database.
    return legacy if not folder.exists() and legacy.exists() else folder


def read_settings(path):
    if not path.exists():
        return ConnectionSettings()
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        row = db.execute("SELECT value FROM metadata WHERE key=?", ("ibkr:settings",)).fetchone()
        return ConnectionSettings.model_validate(json.loads(row[0]) if row else {})
    finally:
        db.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", help="本地数据目录，默认使用 MARKET_DATA_DIR 或应用数据目录")
    parser.add_argument("--port", type=int, help="TWS / IB Gateway Socket API 端口（1024–65535）")
    parser.add_argument("--client-id", type=int, help="客户端 ID（1–999999）")
    parser.add_argument("--mode", choices=("delayed", "live"), help="请求延迟或实时行情")
    enabled = parser.add_mutually_exclusive_group()
    enabled.add_argument("--enable", dest="enabled", action="store_true", default=None)
    enabled.add_argument("--disable", dest="enabled", action="store_false")
    args = parser.parse_args(argv)
    changes = {key: value for key, value in {
        "port": args.port, "client_id": args.client_id, "enabled": args.enabled,
        "delayed": args.mode == "delayed" if args.mode is not None else None,
    }.items() if value is not None}
    path = data_directory(args.data_dir) / "btc-hyperliquid.sqlite3"
    try:
        current = read_settings(path)
        updated = ConnectionSettings.model_validate({**current.model_dump(), **changes})
        if updated != current:
            store = Store(path)
            try:
                # Preserve fields not supplied to this command, including a
                # change saved by another local process after the initial read.
                with store.lock, store.db:
                    store.db.execute("BEGIN IMMEDIATE")
                    latest = ConnectionSettings.model_validate(store.get("ibkr:settings") or {})
                    updated = ConnectionSettings.model_validate({**latest.model_dump(), **changes})
                    store.put("ibkr:settings", updated.model_dump())
            finally:
                store.close()
            print("开发者行情配置已保存；重启行情服务后生效。", file=sys.stderr)
        print(json.dumps(updated.model_dump(), ensure_ascii=False, indent=2))
    except (ValidationError, sqlite3.Error, OSError, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
