import json
from pathlib import Path
import sqlite3
from threading import RLock

from .models import Candle


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        # The database now also contains personal analysis questions/history.
        for local_file in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
            if local_file.exists():
                local_file.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        version = sqlite3.sqlite_version_info
        patched = version >= (3, 51, 3) or version[:2] == (3, 50) and version >= (3, 50, 7) or version[:2] == (3, 44) and version >= (3, 44, 6)
        self.db.execute("PRAGMA journal_mode=" + ("WAL" if patched else "DELETE"))
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS candles (
                symbol TEXT NOT NULL DEFAULT 'BTC',
                timeframe TEXT NOT NULL, time INTEGER NOT NULL, end INTEGER NOT NULL,
                open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
                volume REAL NOT NULL, trades INTEGER NOT NULL, status TEXT NOT NULL,
                received_at INTEGER NOT NULL, revision INTEGER NOT NULL, source TEXT NOT NULL,
                PRIMARY KEY(symbol, timeframe, time)
            );
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);

        """)

        # v1 caches used (timeframe, time); migrate atomically and retain AI tables.
        columns = [row[1] for row in self.db.execute("PRAGMA table_info(candles)")]
        with self.db:
            if "symbol" not in columns:
                self.db.execute("BEGIN IMMEDIATE")
                self.db.execute("ALTER TABLE candles RENAME TO candles_v1")
                self.db.execute("""CREATE TABLE candles (
                    symbol TEXT NOT NULL DEFAULT 'BTC', timeframe TEXT NOT NULL,
                    time INTEGER NOT NULL, end INTEGER NOT NULL,
                    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
                    volume REAL NOT NULL, trades INTEGER NOT NULL, status TEXT NOT NULL,
                    received_at INTEGER NOT NULL, revision INTEGER NOT NULL, source TEXT NOT NULL,
                    PRIMARY KEY(symbol, timeframe, time))""")
                self.db.execute("INSERT INTO candles SELECT 'BTC', * FROM candles_v1")
                self.db.execute("DROP TABLE candles_v1")
            self.db.execute("PRAGMA user_version=2")

    def bars(self, timeframe: str, limit: int = 5000, *, symbol: str = "BTC") -> list[Candle]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM candles WHERE symbol=? AND timeframe=? ORDER BY time DESC LIMIT ?",
                                   (symbol, timeframe, limit)).fetchall()
        return [Candle(**dict(row)) for row in reversed(rows)]

    def merge(self, incoming: list[Candle]) -> int:
        changed = 0
        with self.lock, self.db:
            for bar in incoming:
                row = self.db.execute("SELECT * FROM candles WHERE symbol=? AND timeframe=? AND time=?",
                                      (bar.symbol, bar.timeframe, bar.time)).fetchone()
                if row:
                    previous = Candle(**dict(row))
                    # Only REST may confirm/correct a closed candle. A delayed WS update cannot reopen it.
                    if previous.status == "confirmed" and (bar.source not in {"rest", "ibkr_history"} or bar.status != "confirmed"):
                        continue
                    if bar.source != "ibkr_history" and bar.status != "confirmed" and (bar.trades < previous.trades or bar.volume < previous.volume):
                        continue
                    if bar.source == "ws" and bar.trades == previous.trades and bar.volume == previous.volume:
                        continue
                    fields = ("open", "high", "low", "close", "volume", "trades", "status", "end")
                    different = any(getattr(bar, f) != getattr(previous, f) for f in fields)
                    bar.revision = previous.revision + int(different)
                    changed += int(different)
                else:
                    changed += 1
                values = bar.public()
                self.db.execute("""INSERT OR REPLACE INTO candles
                    (symbol,timeframe,time,end,open,high,low,close,volume,trades,status,received_at,revision,source)
                    VALUES (:symbol,:timeframe,:time,:end,:open,:high,:low,:close,:volume,:trades,:status,:received_at,:revision,:source)
                    """, values)
        return changed

    def put(self, key: str, value: dict):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, json.dumps(value)))

    def get(self, key: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def close(self):
        with self.lock:
            self.db.close()
