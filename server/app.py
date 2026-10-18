import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .catalog import MarketHub
from .watchlists import Watchlists, router as watchlist_router
from .models import TIMEFRAMES
from .storage import Store
from .ai import AnalysisService
from .ai_routes import router as ai_router
from .ibkr import router as ibkr_router
from .intel import IntelService, router as intel_router
from .options_sample import router as options_router
from .exams import router as exams_router
from .exam_runtime import ExamService


def default_data_dir():
    base = Path.home() / "Library/Application Support" if sys.platform == "darwin" else Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    folder = base / "tide"
    legacy = base / "LocalMarket"
    if not folder.exists() and legacy.exists():
        legacy.rename(folder)
    return folder


def create_app(data_dir: Path | None = None, *, live: bool = True):
    @asynccontextmanager
    async def lifespan(app):
        folder = data_dir or Path(os.environ.get("MARKET_DATA_DIR", default_data_dir()))
        store = Store(folder / "btc-hyperliquid.sqlite3")
        app.state.hub = MarketHub(store, live=live)
        app.state.market = app.state.hub.feeds["BTC"]
        app.state.watchlists = Watchlists(store)
        app.state.ai = AnalysisService(app.state.market, folder)
        app.state.intel = IntelService(folder, live=live)
        app.state.exams = ExamService(store, app.state.hub)
        if live:
            await app.state.hub.start()
            await app.state.intel.start()
            await app.state.exams.start()
        try:
            yield
        finally:
            await app.state.exams.stop()
            await app.state.intel.stop()
            await app.state.ai.stop()
            await app.state.hub.stop()
            store.close()

    app = FastAPI(title="LIMEN", version="0.2.0", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
    app.include_router(ai_router)
    app.include_router(watchlist_router)
    app.include_router(ibkr_router)
    app.include_router(intel_router)
    app.include_router(options_router)
    app.include_router(exams_router)

    @app.get("/api/v1/markets")
    async def markets(request: Request):
        return request.app.state.hub.public()

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError):
        # Never reflect submitted credentials in a validation error body.
        return JSONResponse(status_code=422, content={"detail": "请求参数不正确，请检查字段格式和长度。",
                            "fields": [".".join(map(str, item["loc"])) for item in error.errors()]})

    @app.middleware("http")
    async def private_responses(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith(("/api/v1/ai", "/api/v1/intel", "/api/v1/exams")):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/v1/overview")
    async def overview(request: Request, symbol: str = "BTC"):
        return (await request.app.state.hub.get(symbol)).overview()

    @app.get("/api/v1/chart/{timeframe}")
    async def chart(timeframe: str, request: Request, symbol: str = "BTC"):
        if timeframe not in TIMEFRAMES:
            raise HTTPException(422, "Supported timeframes: " + ", ".join(TIMEFRAMES))
        return (await request.app.state.hub.get(symbol)).chart(timeframe)

    @app.get("/api/v1/health")
    async def health(request: Request, symbol: str = "BTC"):
        result = (await request.app.state.hub.get(symbol)).overview()
        return {"service": "limen", **result}

    @app.post("/api/v1/refresh", status_code=202)
    async def refresh(request: Request, symbol: str = "BTC"):
        # A custom header prevents drive-by cross-origin forms from consuming provider quota.
        if request.headers.get("x-local-market") != "1":
            raise HTTPException(403, "Local request header required")
        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
            raise HTTPException(403, "Same-origin requests only")
        (await request.app.state.hub.get(symbol)).sync_event.set()
        return {"status": "scheduled"}

    @app.get("/api/v1/events")
    async def events(request: Request, symbol: str = "BTC"):
        hub = request.app.state.hub
        hub.validate(symbol)
        async def stream():
            feed = None
            try:
                feed = await hub.acquire(symbol)
                while not await request.is_disconnected():
                    yield "data: " + json.dumps(feed.overview(), ensure_ascii=False, allow_nan=False) + "\n\n"
                    await asyncio.sleep(2)
            except HTTPException as error:
                yield "event: service-error\ndata: " + json.dumps({"detail": error.detail}, ensure_ascii=False) + "\n\n"
            finally:
                if feed:
                    hub.release(symbol)
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    dist = Path(__file__).resolve().parents[1] / "frontend/dist"
    if (dist / "assets").exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/favicon.svg", include_in_schema=False)
    async def favicon():
        return FileResponse(dist / "favicon.svg")

    @app.get("/", include_in_schema=False)
    async def index():
        if not (dist / "index.html").exists():
            raise HTTPException(503, "Build frontend first: cd frontend && npm run build")
        return FileResponse(dist / "index.html", headers={"Cache-Control": "no-cache"})

    return app


app = create_app()
