from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_request_id() -> str:
    return uuid.uuid4().hex


def json_log(event: dict[str, Any]) -> None:
    event = dict(event)
    event.setdefault("ts", utc_now_iso())
    print(json.dumps(event, ensure_ascii=False, separators=(",", ":")), flush=True)


@dataclass(frozen=True)
class RequestContext:
    request_id: str
    user_id: str | None = None
    channel: str | None = None
    platform: str | None = None


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        rid = request.headers.get("x-request-id") or new_request_id()
        request.state.request_id = rid
        started = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        json_log(
            {
                "type": "http_access",
                "request_id": rid,
                "method": request.method,
                "path": request.url.path,
                "query": str(request.url.query) if request.url.query else "",
                "status_code": response.status_code,
                "elapsed_ms": elapsed_ms,
            }
        )
        response.headers["X-Request-Id"] = rid
        return response

