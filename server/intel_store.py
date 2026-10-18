"""Persistence for the market-intelligence panel.

Everything lives in ``<data_dir>/intel`` so the existing market database and
its migrations are untouched.  The SQLite file holds small, queryable state
(quota ledger, watchlist, derived events, coverage verdicts) while the bulky
LiqFlow payloads are stored as gzip JSON snapshots on disk.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Iterable

LOGGER = logging.getLogger("tide.intel.store")

DEFAULT_CAP_BYTES = 2 * 1024 ** 3


class QuotaExceeded(RuntimeError):
    def __init__(self, used: int, cap: int):
        super().__init__(f"local quota cap reached ({used}/{cap})")
        self.used = used
        self.cap = cap


def _utc_day(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m%d")


def _now() -> int:
    return int(time.time())


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class IntelStore:
    def __init__(self, data_dir: Path, *, cap_bytes: int = DEFAULT_CAP_BYTES):
        self.root = Path(data_dir) / "intel"
        self.root.mkdir(parents=True, exist_ok=True)
        self.cap_bytes = max(1, int(cap_bytes))
        self.db_path = self.root / "intel.sqlite3"
        self.lock = RLock()
        self.db = sqlite3.connect(self.db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        for local_file in (self.db_path, Path(str(self.db_path) + "-wal"),
                           Path(str(self.db_path) + "-shm")):
            if local_file.exists():
                local_file.chmod(0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS quota (
                month TEXT PRIMARY KEY,
                used INTEGER NOT NULL DEFAULT 0,
                provider_limit INTEGER,
                provider_remaining INTEGER,
                warned INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS quota_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at INTEGER NOT NULL,
                month TEXT NOT NULL,
                kind TEXT NOT NULL,
                endpoint TEXT,
                status INTEGER,
                used INTEGER,
                remaining INTEGER
            );
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                snapshot_time TEXT,
                fetched_at INTEGER NOT NULL,
                rel_path TEXT NOT NULL UNIQUE,
                raw_bytes INTEGER NOT NULL,
                stored_bytes INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS snapshots_kind_time
                ON snapshots(kind, fetched_at DESC);
            CREATE TABLE IF NOT EXISTS watchlist (
                address TEXT PRIMARY KEY COLLATE NOCASE,
                label TEXT,
                source TEXT NOT NULL,
                added_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS fills_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                address TEXT NOT NULL,
                coin TEXT NOT NULL,
                tid TEXT,
                ts INTEGER NOT NULL,
                side TEXT,
                direction TEXT,
                event TEXT NOT NULL,
                size REAL,
                price REAL,
                start_position REAL,
                end_position REAL,
                closed_pnl REAL,
                raw_dir TEXT,
                dedupe TEXT NOT NULL UNIQUE
            );
            CREATE INDEX IF NOT EXISTS fills_events_ts ON fills_events(ts DESC);
            CREATE TABLE IF NOT EXISTS coverage (
                coin TEXT PRIMARY KEY COLLATE NOCASE,
                status TEXT NOT NULL,
                checked_at INTEGER,
                samples INTEGER,
                within_threshold INTEGER,
                max_abs_pct REAL,
                report_path TEXT,
                note TEXT
            );
        """)
        self.db.commit()

    # ---------------------------------------------------------------- basics
    def close(self) -> None:
        with self.lock:
            self.db.close()

    def meta_get(self, key: str, default: str | None = None) -> str | None:
        with self.lock:
            row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def meta_set(self, key: str, value: str) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (key, str(value)))

    # ----------------------------------------------------------- quota ledger
    def quota_begin(self, *, month: str, endpoint: str, limit: int,
                    hard_cap: int) -> dict[str, Any]:
        """Reserve one call.  Raises :class:`QuotaExceeded` at the local cap."""
        now = _now()
        with self.lock, self.db:
            row = self.db.execute("SELECT * FROM quota WHERE month=?", (month,)).fetchone()
            if row is None:
                self.db.execute("INSERT INTO quota(month, used, updated_at) VALUES(?, 0, ?)",
                                (month, now))
                used = 0
                warned = 0
            else:
                used = int(row["used"])
                warned = int(row["warned"])
            if used >= hard_cap:
                self.db.execute("INSERT INTO quota_events(at, month, kind, endpoint, used) "
                                "VALUES(?, ?, 'blocked', ?, ?)", (now, month, endpoint, used))
                raise QuotaExceeded(used, hard_cap)
            used += 1
            warn_at = int(limit * 0.8)
            warning = used >= warn_at and not warned
            if warning:
                warned = 1
                self.db.execute("INSERT INTO quota_events(at, month, kind, endpoint, used) "
                                "VALUES(?, ?, 'warning', ?, ?)", (now, month, endpoint, used))
                LOGGER.warning("LiqFlow quota at %s/%s this month (80%% threshold)", used, limit)
            self.db.execute("UPDATE quota SET used=?, warned=?, updated_at=? WHERE month=?",
                            (used, warned, now, month))
        return {"used": used, "limit": limit, "hard_cap": hard_cap, "warning": warning}

    def quota_record(self, *, month: str, endpoint: str, status: int,
                     provider_limit: int | None, provider_remaining: int | None) -> None:
        """Merge provider headers into the ledger.

        A key can be shared by up to five holders, so ``limit - remaining`` may
        already exceed our local counter.  Take the larger figure: the hard cap
        must be conservative, never optimistic.
        """
        now = _now()
        with self.lock, self.db:
            row = self.db.execute("SELECT * FROM quota WHERE month=?", (month,)).fetchone()
            used = int(row["used"]) if row else 0
            if provider_limit and provider_remaining is not None:
                used = max(used, max(0, int(provider_limit) - int(provider_remaining)))
            self.db.execute(
                "INSERT INTO quota(month, used, provider_limit, provider_remaining, updated_at) "
                "VALUES(?, ?, ?, ?, ?) ON CONFLICT(month) DO UPDATE SET "
                "used=excluded.used, provider_limit=excluded.provider_limit, "
                "provider_remaining=excluded.provider_remaining, updated_at=excluded.updated_at",
                (month, used, provider_limit, provider_remaining, now))
            self.db.execute("INSERT INTO quota_events(at, month, kind, endpoint, status, used, remaining) "
                            "VALUES(?, ?, 'call', ?, ?, ?, ?)",
                            (now, month, endpoint, status, used, provider_remaining))

    def quota_state(self, *, month: str, limit: int, hard_cap: int) -> dict[str, Any]:
        with self.lock:
            row = self.db.execute("SELECT * FROM quota WHERE month=?", (month,)).fetchone()
        used = int(row["used"]) if row else 0
        provider_limit = int(row["provider_limit"]) if row and row["provider_limit"] else limit
        remaining = int(row["provider_remaining"]) if row and row["provider_remaining"] is not None else None
        return {
            "month": month,
            "used": used,
            "limit": provider_limit,
            "hard_cap": hard_cap,
            "remaining": remaining,
            "warning": used >= int(provider_limit * 0.8),
            "exhausted": used >= hard_cap,
            "updated_at": int(row["updated_at"]) if row else None,
        }

    # -------------------------------------------------------------- snapshots
    def _write_atomic(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)

    def save_snapshot(self, kind: str, payload: Any, *, snapshot_time: str | None,
                      fetched_at: int | None = None) -> dict[str, Any]:
        fetched_at = int(fetched_at or _now())
        raw = _json_bytes(payload)
        stored = gzip.compress(raw, compresslevel=6)
        day = _utc_day(fetched_at)
        rel = Path("raw") / kind / day / f"{fetched_at}-{kind}.json.gz"
        path = self.root / rel
        self._write_atomic(path, stored)
        self._write_atomic(self.root / "latest" / f"{kind}.json.gz", stored)
        with self.lock, self.db:
            cursor = self.db.execute(
                "INSERT OR REPLACE INTO snapshots(kind, snapshot_time, fetched_at, rel_path, "
                "raw_bytes, stored_bytes) VALUES(?, ?, ?, ?, ?, ?)",
                (kind, snapshot_time, fetched_at, str(rel), len(raw), len(stored)))
            snapshot_id = cursor.lastrowid
        return {"id": snapshot_id, "kind": kind, "snapshot_time": snapshot_time,
                "fetched_at": fetched_at, "rel_path": str(rel),
                "raw_bytes": len(raw), "stored_bytes": len(stored)}

    def _read_snapshot(self, path: Path) -> Any | None:
        try:
            with gzip.open(path, "rb") as handle:
                return json.loads(handle.read().decode("utf-8"))
        except (OSError, ValueError, EOFError):
            return None

    def latest_snapshot(self, kind: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM snapshots WHERE kind=? ORDER BY fetched_at DESC LIMIT 1",
                (kind,)).fetchone()
        if row:
            payload = self._read_snapshot(self.root / row["rel_path"])
            if payload is not None:
                return {"payload": payload, "meta": dict(row)}
        fallback = self.root / "latest" / f"{kind}.json.gz"
        payload = self._read_snapshot(fallback)
        if payload is None:
            return None
        return {"payload": payload, "meta": {"kind": kind, "fetched_at": int(fallback.stat().st_mtime),
                                             "snapshot_time": None, "rel_path": str(fallback),
                                             "raw_bytes": 0, "stored_bytes": fallback.stat().st_size}}

    def recent_snapshots(self, kind: str, limit: int = 5) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM snapshots WHERE kind=? ORDER BY fetched_at DESC LIMIT ?",
                (kind, limit)).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ watch
    def watch_list(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM watchlist ORDER BY added_at").fetchall()
        return [dict(row) for row in rows]

    def watch_add(self, address: str, *, label: str | None, source: str) -> bool:
        with self.lock, self.db:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO watchlist(address, label, source, added_at) VALUES(?, ?, ?, ?)",
                (address.lower(), label, source, _now()))
        return cursor.rowcount > 0

    def watch_remove(self, address: str) -> bool:
        with self.lock, self.db:
            cursor = self.db.execute("DELETE FROM watchlist WHERE address=?", (address.lower(),))
        return cursor.rowcount > 0

    def event_ts(self, address: str) -> int:
        value = self.meta_get(f"fills_ts:{address.lower()}")
        try:
            return int(value) if value else 0
        except ValueError:
            return 0

    def event_mark(self, address: str, ts: int) -> None:
        self.meta_set(f"fills_ts:{address.lower()}", str(int(ts)))

    # ----------------------------------------------------------------- events
    def add_events(self, address: str, events: Iterable[dict[str, Any]]) -> int:
        inserted = 0
        with self.lock, self.db:
            for event in events:
                tid = str(event.get("tid") or "").strip()
                if tid:
                    dedupe = f"{address.lower()}|{tid}"
                else:
                    digest = hashlib.sha1(_json_bytes(
                        [address.lower(), event.get("coin"), event.get("ts"),
                         event.get("side"), event.get("size"), event.get("price")])).hexdigest()
                    dedupe = f"{address.lower()}|{digest}"
                cursor = self.db.execute(
                    "INSERT OR IGNORE INTO fills_events(address, coin, tid, ts, side, direction, "
                    "event, size, price, start_position, end_position, closed_pnl, raw_dir, dedupe) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (address.lower(), event.get("coin"), tid or None, int(event.get("ts") or 0),
                     event.get("side"), event.get("direction"), event.get("event"),
                     event.get("size"), event.get("price"), event.get("start_position"),
                     event.get("end_position"), event.get("closed_pnl"), event.get("raw_dir"),
                     dedupe))
                inserted += max(0, cursor.rowcount)
        return inserted

    def events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.db.execute(
                "SELECT e.*, w.label AS watch_label FROM fills_events e "
                "LEFT JOIN watchlist w ON w.address = e.address "
                "ORDER BY e.ts DESC, e.id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    # --------------------------------------------------------------- coverage
    def coverage(self, coin: str) -> dict[str, Any]:
        with self.lock:
            row = self.db.execute("SELECT * FROM coverage WHERE coin=?", (coin,)).fetchone()
        if row is None:
            return {"coin": coin.upper(), "status": "unverified", "checked_at": None,
                    "samples": 0, "within_threshold": 0, "max_abs_pct": None,
                    "report_path": None, "note": "尚未抽样核对", "scope": "none"}
        data = dict(row)
        data["scope"] = "global_top25" if str(row["coin"]).upper() == "__GLOBAL__" else "coin"
        return data

    def coverage_effective(self, coin: str) -> dict[str, Any]:
        """Per-coin evidence wins; otherwise fall back to the documented top25 sample."""
        coin = coin.upper()
        own = self.coverage(coin)
        if own["status"] != "unverified" or coin.startswith("__"):
            return own
        broad = self.coverage("__GLOBAL__")
        if broad["status"] != "unverified":
            merged = dict(broad)
            merged["coin"] = coin
            merged["scope"] = "global_top25"
            return merged
        return own

    def set_coverage(self, coin: str, *, status: str, samples: int, within_threshold: int,
                     max_abs_pct: float | None, report_path: str | None,
                     note: str | None = None) -> None:
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO coverage(coin, status, checked_at, samples, within_threshold, "
                "max_abs_pct, report_path, note) VALUES(?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(coin) DO UPDATE SET status=excluded.status, "
                "checked_at=excluded.checked_at, samples=excluded.samples, "
                "within_threshold=excluded.within_threshold, max_abs_pct=excluded.max_abs_pct, "
                "report_path=excluded.report_path, note=excluded.note",
                (coin.upper(), status, _now(), samples, within_threshold, max_abs_pct,
                 report_path, note))

    # ---------------------------------------------------------------- storage
    def storage_usage(self) -> int:
        total = 0
        for path in self.root.rglob("*"):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                continue
        return total

    def storage_state(self) -> dict[str, Any]:
        used = self.storage_usage()
        return {"used_bytes": used, "cap_bytes": self.cap_bytes,
                "used_ratio": round(used / self.cap_bytes, 4) if self.cap_bytes else 0}

    def _drop_snapshot_rows(self, rel_paths: list[str]) -> None:
        if not rel_paths:
            return
        with self.lock, self.db:
            self.db.executemany("DELETE FROM snapshots WHERE rel_path=?", [(p,) for p in rel_paths])

    def downsample(self, *, older_than_hours: int = 48) -> int:
        """Keep the newest snapshot per kind/hour for data older than the window."""
        cutoff = time.time() - older_than_hours * 3600
        groups: dict[tuple[str, str, str], list[Path]] = {}
        for path in (self.root / "raw").rglob("*.json.gz"):
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_mtime >= cutoff:
                continue
            parts = path.parts
            try:
                index = parts.index("raw")
                kind, day = parts[index + 1], parts[index + 2]
            except (ValueError, IndexError):
                continue
            hour = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).strftime("%Y%m%d%H")
            groups.setdefault((kind, day, hour), []).append(path)
        removed_paths: list[Path] = []
        for files in groups.values():
            if len(files) <= 1:
                continue
            files.sort(key=lambda p: p.stat().st_mtime)
            removed_paths.extend(files[:-1])
        dropped = 0
        for path in removed_paths:
            try:
                size = path.stat().st_size
                path.unlink()
                dropped += size
                self._drop_snapshot_rows([str(path.relative_to(self.root))])
            except OSError:
                continue
        return dropped

    def prune(self, *, cap_bytes: int | None = None) -> dict[str, Any]:
        """Enforce the disk cap by deleting the oldest raw snapshots.

        ``latest/`` is never touched, so the panel always keeps one readable
        snapshot even after aggressive pruning.
        """
        cap = int(cap_bytes or self.cap_bytes)
        now = time.time()
        last = self.meta_get("last_downsample")
        if not last or now - float(last) > 3600:
            self.downsample(older_than_hours=48)
            self.meta_set("last_downsample", str(int(now)))

        before = self.storage_usage()
        removed = 0
        removed_files = 0
        if before > cap:
            target = int(cap * 0.8)
            files = sorted((self.root / "raw").rglob("*.json.gz"),
                           key=lambda p: p.stat().st_mtime)
            for path in files:
                if self.storage_usage() <= target:
                    break
                try:
                    size = path.stat().st_size
                    rel = str(path.relative_to(self.root))
                    path.unlink()
                    self._drop_snapshot_rows([rel])
                    removed += size
                    removed_files += 1
                except OSError:
                    continue
        after = self.storage_usage()
        if after > cap:
            LOGGER.warning("intel storage still above cap: %s > %s bytes", after, cap)
        return {"before_bytes": before, "after_bytes": after, "cap_bytes": cap,
                "removed_bytes": removed, "removed_files": removed_files}
