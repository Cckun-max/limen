import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .ai_store import ModelConfig
from .frameworks import FrameworkDraft, FrameworkUpdate

router = APIRouter(prefix="/api/v1/ai")


def local_write(request: Request):
    if request.headers.get("x-local-market") != "1":
        raise HTTPException(403, "缺少本地请求标识")
    origin = request.headers.get("origin")
    if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
        raise HTTPException(403, "仅允许同源请求")


class ConfigUpdate(ModelConfig):
    clear_key: bool = False


class CaptureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timeframe: str
    symbol: str = "BTC"
    framework_id: str | None = Field(default=None, min_length=1, max_length=64)
    framework_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def framework_pair(self):
        if (self.framework_id is None) != (self.framework_version is None):
            raise ValueError("框架与版本需同时指定")
        return self


class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    request_id: UUID
    snapshot_id: UUID | None = None
    parent_id: UUID | None = None
    question: str = Field(min_length=1, max_length=2000)


@router.get("/config")
async def config(request: Request):
    return request.app.state.ai.config.read().public()


@router.put("/config", dependencies=[Depends(local_write)])
async def save_config(body: ConfigUpdate, request: Request):
    return request.app.state.ai.config.save(ModelConfig(**body.model_dump(exclude={"clear_key"})), clear_key=body.clear_key)


@router.post("/snapshots", dependencies=[Depends(local_write)])
async def capture(body: CaptureRequest, request: Request):
    market = await request.app.state.hub.get(body.symbol)
    return request.app.state.ai.capture(body.timeframe, market, body.framework_id, body.framework_version)


@router.get("/frameworks")
async def frameworks(request: Request):
    return request.app.state.ai.frameworks.list()


@router.post("/frameworks", status_code=201, dependencies=[Depends(local_write)])
async def create_framework(body: FrameworkDraft, request: Request):
    return request.app.state.ai.frameworks.create(body)


@router.put("/frameworks/{framework_id}", dependencies=[Depends(local_write)])
async def update_framework(framework_id: str, body: FrameworkUpdate, request: Request):
    return request.app.state.ai.frameworks.update(framework_id, body)


@router.get("/frameworks/{framework_id}/versions")
async def framework_versions(framework_id: str, request: Request):
    return request.app.state.ai.frameworks.versions(framework_id)


@router.post("/analyses", status_code=202, dependencies=[Depends(local_write)])
async def analyze(body: AnalyzeRequest, request: Request):
    return request.app.state.ai.start(str(body.request_id), str(body.snapshot_id) if body.snapshot_id else None,
                                      str(body.parent_id) if body.parent_id else None, body.question)


@router.get("/analyses")
async def history(request: Request):
    return request.app.state.ai.store.list_runs()


@router.get("/analyses/{run_id}")
async def run(run_id: UUID, request: Request):
    return request.app.state.ai.public_run(str(run_id))


@router.post("/analyses/{run_id}/cancel", dependencies=[Depends(local_write)])
async def cancel(run_id: UUID, request: Request):
    return await request.app.state.ai.cancel(str(run_id))


@router.get("/analyses/{run_id}/export")
async def export(run_id: UUID, request: Request):
    run = request.app.state.ai.public_run(str(run_id))
    return Response(json.dumps(run, ensure_ascii=False, indent=2), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="market-analysis-{run_id}.json"',
                             "Cache-Control": "no-store"})
