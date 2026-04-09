from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.observability import json_log
from app.parser_agent import run_once
from app.queue_bus import publish_parser_job


router = APIRouter(prefix="/api/parser", tags=["parser"])


class RunParserResponse(BaseModel):
    ok: bool
    request_id: str
    ts_utc: str
    sources_total: int
    fetched: int
    changed: int
    change_package_path: str


class RunParserRequest(BaseModel):
    limit: int | None = Field(default=None, ge=1, le=200)


@router.post("/run", response_model=RunParserResponse)
async def run_parser(request: Request, body: RunParserRequest) -> RunParserResponse:
    rid = getattr(request.state, "request_id", uuid.uuid4().hex)
    res = await run_once(limit=body.limit)
    json_log({"type": "parser_run_api", "request_id": rid, "changed": res.get("changed"), "limit": body.limit})
    return RunParserResponse(
        ok=True,
        request_id=rid,
        ts_utc=str(res["ts_utc"]),
        sources_total=int(res["sources_total"]),
        fetched=int(res["fetched"]),
        changed=int(res["changed"]),
        change_package_path=str(res["change_package_path"]),
    )


class EnqueueParserResponse(BaseModel):
    ok: bool
    request_id: str
    job_id: str


@router.post("/enqueue", response_model=EnqueueParserResponse)
async def enqueue_parser(request: Request, body: RunParserRequest) -> EnqueueParserResponse:
    rid = getattr(request.state, "request_id", uuid.uuid4().hex)
    job_id = await publish_parser_job(payload={"limit": body.limit})
    json_log({"type": "parser_enqueued", "request_id": rid, "job_id": job_id, "limit": body.limit})
    return EnqueueParserResponse(ok=True, request_id=rid, job_id=job_id)

