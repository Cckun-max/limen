"""Local model configuration and immutable analysis inputs."""
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import now_ms
from .storage import Store


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    base_url: str = Field(default="", max_length=500)
    model: str = Field(default="", max_length=160)
    api_key: str = Field(default="", max_length=4096, repr=False)
    max_tokens: int = Field(default=4096, ge=512, le=16384)
    token_parameter: str = "max_completion_tokens"

    @field_validator("token_parameter")
    @classmethod
    def token_field(cls, value):
        if value not in {"max_completion_tokens", "max_tokens"}:
            raise ValueError("Unsupported token parameter")
        return value

    @field_validator("base_url")
    @classmethod
    def endpoint(cls, value):
        value = value.rstrip("/")
        if not value:
            return value
        url = urlsplit(value)
        local = url.hostname in {"localhost", "127.0.0.1", "::1"}
        if not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("Use an API base URL without credentials, query or fragment")
        if url.scheme != "https" and not (url.scheme == "http" and local):
            raise ValueError("Use HTTPS, or HTTP on localhost")
        if value.endswith("/chat/completions"):
            raise ValueError("Use the base URL, without /chat/completions")
        return value

    @property
    def ready(self):
        local = urlsplit(self.base_url).hostname in {"localhost", "127.0.0.1", "::1"}
        return bool(self.base_url and self.model and (self.api_key or local))

    def public(self):
        return {**self.model_dump(exclude={"api_key"}), "has_key": bool(self.api_key),
                "ready": self.ready, "protocol": "chat_completions"}


class ConfigFile:
    def __init__(self, folder: Path):
        self.folder = folder / "ai-private"
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.folder.chmod(0o700)
        self.path = self.folder / "model.json"

    def read(self):
        if not self.path.exists():
            return ModelConfig()
        return ModelConfig.model_validate_json(self.path.read_text())

    def save(self, config: ModelConfig, *, clear_key=False):
        old = self.read()
        # An empty key preserves it only for the same destination. Never silently
        # forward an existing credential to a newly configured host/path.
        if clear_key:
            config.api_key = ""
        elif not config.api_key and config.base_url == old.base_url:
            config.api_key = old.api_key
        fd, name = tempfile.mkstemp(dir=self.folder, prefix=".model-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as file:
                json.dump(config.model_dump(), file, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        return config.public()


class AnalysisStore:
    def __init__(self, store: Store):
        self.store = store
        with store.lock, store.db:
            store.db.executescript("""
                CREATE TABLE IF NOT EXISTS ai_snapshots (
                    id TEXT PRIMARY KEY, created_at INTEGER NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ai_runs (
                    id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL, parent_id TEXT,
                    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                    status TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ai_runs_created ON ai_runs(created_at DESC);
            """)
        # No task survives a process restart. Retain the input for investigation.
        for item in self.list_runs(status="running"):
            run = self.run(item["id"])
            run.update(status="failed", error="本地服务已重启，本次分析中断。可重新发起分析。")
            self.update(run)

    def snapshot(self, snapshot_id):
        with self.store.lock:
            row = self.store.db.execute("SELECT payload FROM ai_snapshots WHERE id=?", (snapshot_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def add_snapshot(self, snapshot):
        with self.store.lock, self.store.db:
            self.store.db.execute("INSERT INTO ai_snapshots VALUES (?,?,?)",
                                  (snapshot["id"], snapshot["as_of"], json.dumps(snapshot, ensure_ascii=False, allow_nan=False)))

    def add_run(self, run):
        with self.store.lock, self.store.db:
            self.store.db.execute("INSERT INTO ai_runs VALUES (?,?,?,?,?,?,?)",
                                  (run["id"], run["snapshot_id"], run["parent_id"], run["created_at"],
                                   run["updated_at"], run["status"], json.dumps(run, ensure_ascii=False)))

    def update(self, run):
        run["updated_at"] = now_ms()
        with self.store.lock, self.store.db:
            self.store.db.execute("UPDATE ai_runs SET status=?,updated_at=?,payload=? WHERE id=?",
                                  (run["status"], run["updated_at"], json.dumps(run, ensure_ascii=False), run["id"]))

    def run(self, run_id):
        with self.store.lock:
            row = self.store.db.execute("SELECT payload FROM ai_runs WHERE id=?", (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def list_runs(self, *, status=None, limit=100):
        sql = "SELECT payload FROM ai_runs"
        params = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.store.lock:
            rows = self.store.db.execute(sql, params).fetchall()
        keys = ("id", "snapshot_id", "parent_id", "created_at", "updated_at", "status", "symbol", "timeframe", "question", "error", "model")
        return [{key: item.get(key, "BTC" if key == "symbol" else None) for key in keys} for item in (json.loads(row[0]) for row in rows)]
