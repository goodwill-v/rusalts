from __future__ import annotations

import json
import re
import secrets
import uuid
from datetime import date
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import PlainTextResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

from app import config
from app.content_excerpt import title_fallback_from_site_text
from app.content_store import (
    ContentItem,
    archive_item,
    item_exists,
    load_item,
    list_items,
    next_publication_id,
    purge_archived_older_than_days,
    save_item,
    set_status,
    update_item,
)
from app.content_publish_flow import approve_publication_by_id
from app.observability import json_log
from app.publishers.site import get_site_markdown, publish_to_site, remove_site_publications, set_site_publications_pinned
from app.queue_bus import publish_content_job
from app.workers.content_worker import refine_corporate_item_by_id


router = APIRouter(prefix="/api/content", tags=["content"])

_ID_RE = re.compile(r"^\d{5}$")

_basic = HTTPBasic()


def _require_admin_auth(credentials: HTTPBasicCredentials = Depends(_basic)) -> str:
    ok_user = secrets.compare_digest(credentials.username or "", "admin")
    ok_pass = secrets.compare_digest(credentials.password or "", "20rusalt13")
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


class SubmitContentRequest(BaseModel):
    title: str = Field(..., min_length=3, max_length=200)
    site_text: str = Field(..., min_length=20, max_length=50_000)
    vk_text: str = Field(..., min_length=10, max_length=20_000)
    internal_note: str = Field(..., min_length=0, max_length=50_000)
    sources: list[str] = Field(default_factory=list, max_length=50)
    publish_date: date | None = None


class SubmitContentResponse(BaseModel):
    ok: bool
    publication_id: str
    status: str
    request_id: str


@router.post("/submit", response_model=SubmitContentResponse)
async def submit_content(request: Request, body: SubmitContentRequest) -> SubmitContentResponse:
    config.ensure_data_dirs()
    rid = getattr(request.state, "request_id", uuid.uuid4().hex)

    pub_id = next_publication_id()
    item = ContentItem(
        publication_id=pub_id,
        created_at_utc=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        status="pending",
        title=body.title.strip(),
        site_text=body.site_text.strip(),
        vk_text=body.vk_text.strip(),
        internal_note=(body.internal_note or "").strip(),
        sources=[s.strip() for s in body.sources if s and s.strip()],
    )

    save_item(item)

    # Local MVP mode: skip email workflow and auto-approve + publish to site.
    if config.CONTENT_APPROVAL_MODE == "local_autoapprove":
        set_status(pub_id, status="approved", message_id="local_autoapprove")
        item = load_item(pub_id)
        try:
            pub_at, url_path = publish_to_site(item)
            update_item(pub_id, site_published_at_utc=pub_at, site_url=url_path, last_publish_error=None)
        except Exception as e:
            update_item(pub_id, last_publish_error=str(e))
        json_log({"type": "content_autoapproved_local", "request_id": rid, "publication_id": pub_id})
        return SubmitContentResponse(ok=True, publication_id=pub_id, status="approved", request_id=rid)

    # Web approvals (no email): item stays pending and is reviewed on /publapprov/.
    json_log({"type": "content_submitted_pending_web", "request_id": rid, "publication_id": pub_id})
    return SubmitContentResponse(ok=True, publication_id=pub_id, status=item.status, request_id=rid)


class QueueItem(BaseModel):
    publication_id: str
    created_at_utc: str
    status: str
    title: str
    site_text: str
    vk_text: str
    sources: list[str] = []
    pinned: bool = False
    site_published_at_utc: str | None = None
    vk_published_at_utc: str | None = None
    last_publish_error: str | None = None
    vk_post_url: str | None = None


class QueueResponse(BaseModel):
    ok: bool
    request_id: str
    items: list[QueueItem]


@router.get("/queue", response_model=QueueResponse, dependencies=[Depends(_require_admin_auth)])
async def queue(request: Request) -> QueueResponse:
    rid = getattr(request.state, "request_id", uuid.uuid4().hex)
    items = list_items(statuses={"pending", "needs_edit"})
    return QueueResponse(
        ok=True,
        request_id=rid,
        items=[
            QueueItem(
                publication_id=it.publication_id,
                created_at_utc=it.created_at_utc,
                status=it.status,
                title=it.title,
                site_text=it.site_text,
                vk_text=it.vk_text,
                sources=it.sources or [],
                pinned=bool(getattr(it, "pinned", False)),
                site_published_at_utc=it.site_published_at_utc,
                vk_published_at_utc=it.vk_published_at_utc,
                last_publish_error=it.last_publish_error,
                vk_post_url=it.vk_post_url,
            )
            for it in items
        ],
    )


class UpdateQueueItemRequest(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    site_text: str | None = Field(default=None, min_length=20, max_length=50_000)
    vk_text: str | None = Field(default=None, max_length=20_000)


class SimpleOkResponse(BaseModel):
    ok: bool
    request_id: str


class ApproveResponse(SimpleOkResponse):
    publication_id: str
    site_published: bool
    vk_published: bool
    vk_post_url: str | None = None
    last_publish_error: str | None = None


async def _approve_and_publish_item(*, rid: str, publication_id: str) -> ApproveResponse:
    """Общая логика кнопки «Опубликовать» в веб-очереди и мгновенной публикации с /publapprov."""
    if not _ID_RE.match(publication_id):
        raise HTTPException(status_code=400, detail="Некорректный ID")
    if not item_exists(publication_id):
        raise HTTPException(status_code=404, detail="Не найдено")
    data = await approve_publication_by_id(request_id=rid, publication_id=publication_id)
    json_log(
        {
            "type": "content_approved_web",
            "request_id": rid,
            "publication_id": publication_id,
            "site_published": data.get("site_published"),
            "vk_published": data.get("vk_published"),
            "last_publish_error": data.get("last_publish_error"),
        }
    )
    return ApproveResponse(**data)


@router.put("/queue/{publication_id}", response_model=SimpleOkResponse, dependencies=[Depends(_require_admin_auth)])
async def update_queue_item(request: Request, publication_id: str, body: UpdateQueueItemRequest) -> SimpleOkResponse:
    rid = getattr(request.state, "request_id", uuid.uuid4().hex)
    if not _ID_RE.match(publication_id):
        raise HTTPException(status_code=400, detail="Некорректный ID")
    if not item_exists(publication_id):
        raise HTTPException(status_code=404, detail="Не найдено")
    fields: dict = {}
    it = load_item(publication_id)
    new_site = body.site_text.strip() if body.site_text is not None else None
    new_vk = body.vk_text.strip() if body.vk_text is not None else None
    if new_site is not None:
        fields["site_text"] = new_site
    if new_vk is not None:
        site_for_vk = (new_site if new_site is not None else it.site_text).strip()
        vk_eff = new_vk if len(new_vk) >= 10 else site_for_vk
        fields["vk_text"] = vk_eff
    if body.title is not None:
        t = body.title.strip()
        fields["title"] = t or title_fallback_from_site_text(new_site or it.site_text)
    elif new_site is not None:
        fields["title"] = title_fallback_from_site_text(new_site)
    if fields:
        update_item(publication_id, **fields)
        json_log({"type": "content_queue_item_updated", "request_id": rid, "publication_id": publication_id, "fields": sorted(fields.keys())})
    return SimpleOkResponse(ok=True, request_id=rid)


@router.post("/queue/{publication_id}/pin", response_model=SimpleOkResponse, dependencies=[Depends(_require_admin_auth)])
async def toggle_pin(request: Request, publication_id: str) -> SimpleOkResponse:
    rid = getattr(request.state, "request_id", uuid.uuid4().hex)
    if not _ID_RE.match(publication_id):
        raise HTTPException(status_code=400, detail="Некорректный ID")
    if not item_exists(publication_id):
        raise HTTPException(status_code=404, detail="Не найдено")
    it = load_item(publication_id)
    update_item(publication_id, pinned=(not bool(getattr(it, "pinned", False))))
    json_log({"type": "content_queue_item_pinned_toggled", "request_id": rid, "publication_id": publication_id})
    return SimpleOkResponse(ok=True, request_id=rid)


@router.post("/queue/{publication_id}/approve", response_model=ApproveResponse, dependencies=[Depends(_require_admin_auth)])
async def approve(request: Request, publication_id: str) -> ApproveResponse:
    rid = getattr(request.state, "request_id", uuid.uuid4().hex)
    return await _approve_and_publish_item(rid=rid, publication_id=publication_id)


class ApproveAllResponse(SimpleOkResponse):
    approved: int
    failed: int
    items: list[ApproveResponse]


@router.post("/queue/approve-all", response_model=ApproveAllResponse, dependencies=[Depends(_require_admin_auth)])
async def approve_all(request: Request) -> ApproveAllResponse:
    rid = getattr(request.state, "request_id", uuid.uuid4().hex)
    pending = list_items(statuses={"pending", "needs_edit"})
    out: list[ApproveResponse] = []
    approved = 0
    failed = 0
    for it in pending:
        try:
            res = await approve(request, it.publication_id)
            out.append(res)
            if res.last_publish_error:
                failed += 1
            else:
                approved += 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            out.append(
                ApproveResponse(
                    ok=False,
                    request_id=rid,
                    publication_id=it.publication_id,
                    site_published=False,
                    vk_published=False,
                    last_publish_error=str(e),
                )
            )

    return ApproveAllResponse(ok=True, request_id=rid, approved=approved, failed=failed, items=out)


@router.post("/queue/{publication_id}/cancel", response_model=SimpleOkResponse, dependencies=[Depends(_require_admin_auth)])
async def cancel(request: Request, publication_id: str) -> SimpleOkResponse:
    rid = getattr(request.state, "request_id", uuid.uuid4().hex)
    if not _ID_RE.match(publication_id):
        raise HTTPException(status_code=400, detail="Некорректный ID")
    if not item_exists(publication_id):
        raise HTTPException(status_code=404, detail="Не найдено")
    set_status(publication_id, status="rejected", message_id="web_cancelled")
    archive_item(publication_id)
    purged = purge_archived_older_than_days(30)
    if purged:
        json_log({"type": "content_archive_purged", "request_id": rid, "removed": purged})
    json_log({"type": "content_cancelled_web", "request_id": rid, "publication_id": publication_id})
    return SimpleOkResponse(ok=True, request_id=rid)


def _normalize_pub_ids(raw: list[str]) -> list[str]:
    out: list[str] = []
    for x in raw or []:
        s = str(x or "").strip()
        if _ID_RE.match(s):
            out.append(s)
    return out


class SiteBatchIdsRequest(BaseModel):
    publication_ids: list[str] = Field(default_factory=list, max_length=500)


class SiteBatchPinRequest(SiteBatchIdsRequest):
    pinned: bool = True


class SiteBatchOpResponse(SimpleOkResponse):
    affected: int


@router.post("/site/batch-delete", response_model=SiteBatchOpResponse, dependencies=[Depends(_require_admin_auth)])
async def site_batch_delete(request: Request, body: SiteBatchIdsRequest) -> SiteBatchOpResponse:
    rid = getattr(request.state, "request_id", uuid.uuid4().hex)
    ids = _normalize_pub_ids(body.publication_ids)
    removed_count, _ = remove_site_publications(ids)
    json_log({"type": "content_site_batch_delete", "request_id": rid, "removed": removed_count, "ids": ids})
    return SiteBatchOpResponse(ok=True, request_id=rid, affected=removed_count)


@router.post("/site/batch-pin", response_model=SiteBatchOpResponse, dependencies=[Depends(_require_admin_auth)])
async def site_batch_pin(request: Request, body: SiteBatchPinRequest) -> SiteBatchOpResponse:
    rid = getattr(request.state, "request_id", uuid.uuid4().hex)
    ids = _normalize_pub_ids(body.publication_ids)
    n = set_site_publications_pinned(ids, pinned=body.pinned)
    json_log({"type": "content_site_batch_pin", "request_id": rid, "updated": n, "pinned": body.pinned, "ids": ids})
    return SiteBatchOpResponse(ok=True, request_id=rid, affected=n)


class CorporateNewsRequest(BaseModel):
    title: str = Field(default="", max_length=200)
    site_text: str = Field(..., min_length=20, max_length=50_000)
    vk_text: str = Field(default="", max_length=20_000)
    internal_note: str = Field(default="", max_length=50_000)
    sources: list[str] = Field(default_factory=list, max_length=50)
    pinned: bool = False


class CorporateSaveResponse(SimpleOkResponse):
    publication_id: str


@router.post("/corporate/save", response_model=CorporateSaveResponse, dependencies=[Depends(_require_admin_auth)])
async def corporate_save(request: Request, body: CorporateNewsRequest) -> CorporateSaveResponse:
    """Черновик в очередь согласования + задача content-worker (нормализация полей)."""
    rid = getattr(request.state, "request_id", uuid.uuid4().hex)
    config.ensure_data_dirs()
    pub_id = next_publication_id()
    site_s = body.site_text.strip()
    title_e = (body.title or "").strip() or title_fallback_from_site_text(site_s)
    vk_s = (body.vk_text or "").strip() or site_s
    item = ContentItem(
        publication_id=pub_id,
        created_at_utc=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        status="pending",
        title=title_e,
        site_text=site_s,
        vk_text=vk_s,
        internal_note=("corporate_portal\n" + (body.internal_note or "").strip()).strip(),
        sources=[s.strip() for s in body.sources if s and str(s).strip()],
        pinned=bool(body.pinned),
    )
    save_item(item)
    await publish_content_job(job_type="content.corporate_draft", payload={"publication_id": pub_id})
    json_log({"type": "content_corporate_saved", "request_id": rid, "publication_id": pub_id})
    return CorporateSaveResponse(ok=True, request_id=rid, publication_id=pub_id)


@router.post("/corporate/publish", response_model=ApproveResponse, dependencies=[Depends(_require_admin_auth)])
async def corporate_publish(request: Request, body: CorporateNewsRequest) -> ApproveResponse:
    """Сразу на сайт и ВК; при пустом vk_text используется текст сайта для обоих каналов."""
    rid = getattr(request.state, "request_id", uuid.uuid4().hex)
    config.ensure_data_dirs()
    vk = (body.vk_text or "").strip() or body.site_text.strip()
    if len(vk) < 10:
        raise HTTPException(status_code=400, detail="Текст для публикации слишком короткий")
    pub_id = next_publication_id()
    site_s = body.site_text.strip()
    title_e = (body.title or "").strip() or title_fallback_from_site_text(site_s)
    item = ContentItem(
        publication_id=pub_id,
        created_at_utc=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        status="pending",
        title=title_e,
        site_text=site_s,
        vk_text=vk,
        internal_note=("corporate_portal_publish\n" + (body.internal_note or "").strip()).strip(),
        sources=[s.strip() for s in body.sources if s and str(s).strip()],
        pinned=bool(body.pinned),
    )
    save_item(item)
    json_log({"type": "content_corporate_publish", "request_id": rid, "publication_id": pub_id})
    ok, err = await refine_corporate_item_by_id(pub_id)
    if not ok:
        raise HTTPException(status_code=502, detail=f"Не удалось подготовить текст для публикации: {err}")
    data = await approve_publication_by_id(request_id=rid, publication_id=pub_id)
    json_log({"type": "content_corporate_publish_done", "request_id": rid, "publication_id": pub_id})
    return ApproveResponse(**data)


@router.get("/site/index")
async def site_index() -> dict:
    """Index of published site news (for UI)."""
    path = config.CONTENT_PUBLISHED_SITE_INDEX_PATH
    if not path.exists():
        return {"items": []}
    try:
        import json

        return {"items": json.loads(path.read_text(encoding="utf-8"))}
    except Exception:
        return {"items": []}


@router.get("/site/{publication_id}", response_class=PlainTextResponse)
async def site_item(publication_id: str) -> PlainTextResponse:
    if not _ID_RE.match(publication_id):
        raise HTTPException(status_code=400, detail="Некорректный ID")
    try:
        text = get_site_markdown(publication_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Не найдено") from None
    return PlainTextResponse(text, media_type="text/markdown; charset=utf-8")

