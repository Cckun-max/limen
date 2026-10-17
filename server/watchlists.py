from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .ai_routes import local_write
from .models import now_ms


class Watchlists:
    def __init__(self, store):
        self.store = store
        store.db.executescript("""
            CREATE TABLE IF NOT EXISTS watchlists (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, created_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS watchlist_items (group_id TEXT NOT NULL, symbol TEXT NOT NULL, added_at INTEGER NOT NULL,
                                                       PRIMARY KEY(group_id, symbol));
        """)
        with store.lock, store.db:
            store.db.execute("INSERT OR IGNORE INTO watchlists VALUES ('default', '默认自选', ?)", (now_ms(),))

    def all(self):
        with self.store.lock:
            groups = self.store.db.execute("SELECT * FROM watchlists ORDER BY created_at, id").fetchall()
            items = self.store.db.execute("SELECT * FROM watchlist_items ORDER BY added_at, symbol").fetchall()
        return [{**dict(group), "symbols": [r["symbol"] for r in items if r["group_id"] == group["id"]]} for group in groups]

    def require(self, group_id):
        if not self.store.db.execute("SELECT 1 FROM watchlists WHERE id=?", (group_id,)).fetchone():
            raise HTTPException(404, "自选分组不存在，请刷新列表。")

    def save_group(self, name, group_id=None):
        with self.store.lock, self.store.db:
            if group_id:
                self.require(group_id)
            if self.store.db.execute("SELECT 1 FROM watchlists WHERE name=? AND id<>?", (name, group_id or "")).fetchone():
                raise HTTPException(409, "已有同名分组")
            if group_id:
                self.store.db.execute("UPDATE watchlists SET name=? WHERE id=?", (name, group_id))
            else:
                if self.store.db.execute("SELECT count(*) FROM watchlists").fetchone()[0] >= 30:
                    raise HTTPException(422, "最多创建 30 个分组")
                self.store.db.execute("INSERT INTO watchlists VALUES (?,?,?)", (str(uuid4()), name, now_ms()))
        return self.all()

    def delete_group(self, group_id):
        if group_id == "default":
            raise HTTPException(422, "默认分组不能删除，可删除其中的币种。")
        with self.store.lock, self.store.db:
            self.require(group_id)
            # Deleting a custom group moves its members to the default group.
            self.store.db.execute("INSERT OR IGNORE INTO watchlist_items SELECT 'default', symbol, added_at FROM watchlist_items WHERE group_id=?", (group_id,))
            self.store.db.execute("DELETE FROM watchlist_items WHERE group_id=?", (group_id,))
            self.store.db.execute("DELETE FROM watchlists WHERE id=?", (group_id,))
        return self.all()

    def item(self, group_id, symbol, remove=False):
        with self.store.lock, self.store.db:
            self.require(group_id)
            if remove:
                self.store.db.execute("DELETE FROM watchlist_items WHERE group_id=? AND symbol=?", (group_id, symbol))
            else:
                self.store.db.execute("INSERT OR IGNORE INTO watchlist_items VALUES (?,?,?)", (group_id, symbol, now_ms()))
        return self.all()


router = APIRouter(prefix="/api/v1/watchlists")


class GroupBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=30)


@router.get("")
async def lists(request: Request):
    return request.app.state.watchlists.all()


@router.post("", dependencies=[Depends(local_write)])
async def create(body: GroupBody, request: Request):
    return request.app.state.watchlists.save_group(body.name)


@router.patch("/{group_id}", dependencies=[Depends(local_write)])
async def rename(group_id: str, body: GroupBody, request: Request):
    return request.app.state.watchlists.save_group(body.name, group_id)


@router.delete("/{group_id}", dependencies=[Depends(local_write)])
async def delete(group_id: str, request: Request):
    return request.app.state.watchlists.delete_group(group_id)


@router.put("/{group_id}/items/{symbol}", dependencies=[Depends(local_write)])
async def add(group_id: str, symbol: str, request: Request):
    request.app.state.hub.validate(symbol)
    return request.app.state.watchlists.item(group_id, symbol)


@router.delete("/{group_id}/items/{symbol}", dependencies=[Depends(local_write)])
async def remove(group_id: str, symbol: str, request: Request):
    return request.app.state.watchlists.item(group_id, symbol, remove=True)
