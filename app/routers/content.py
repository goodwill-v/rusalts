from __future__ import annotations

import re
import uuid
from datetime import date
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from app import config
from app.chief_mail import poll_chief_inbox, send_to_chief
from app.content_store import (
    ContentItem,
    archive_item,
    item_exists,
    load_item,
    next_publication_id,
    purge_archived_older_than_days,
    save_item,
    set_status,
    update_item,
)
from app.observability import json_log
from app.publishers.site import get_site_markdown, publish_to_site
from app.publishers.vk import publish_to_vk


router = APIRouter(prefix="/api/content", tags=["content"])

_ID_RE = re.compile(r"^\d{5}$")


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

    human_date = (body.publish_date.isoformat() if body.publish_date else "сегодня")
    sources_block = "\n".join(f"- {s}" for s in item.sources) if item.sources else "- (нет)"

    mail_subject = f"Публикация {human_date} ({pub_id}) — на согласование"
    mail_body = (
        "Нужна проверка публикации по ТЗ.\n\n"
        "Ответьте письмом в формате:\n"
        "- ДА: «Текст публикации»\n"
        "- НЕТ: «Текст публикации»\n"
        "- РЕДАКТИРОВАТЬ, пояснение: «Текст публикации»\n\n"
        f"ID публикации: ({pub_id})\n"
        f"Заголовок: {item.title}\n\n"
        "=== САЙТ ===\n"
        f"{item.site_text}\n\n"
        "=== VK ===\n"
        f"{item.vk_text}\n\n"
        "=== ВНУТРЕННЯЯ ЗАМЕТКА ===\n"
        f"{item.internal_note or '(нет)'}\n\n"
        "=== ИСТОЧНИКИ ===\n"
        f"{sources_block}\n"
    )
    try:
        send_to_chief(subject=mail_subject, body=mail_body)
    except Exception as e:
        json_log({"type": "chief_mail_send_failed", "request_id": rid, "publication_id": pub_id, "error": str(e)})
        raise HTTPException(status_code=500, detail=f"Не удалось отправить письмо Chief: {e}") from e

    json_log({"type": "content_submitted", "request_id": rid, "publication_id": pub_id, "to": config.CHIEF_EMAIL_TO})
    return SubmitContentResponse(ok=True, publication_id=pub_id, status=item.status, request_id=rid)


class PollChiefResponseItem(BaseModel):
    publication_id: str
    applied: bool
    new_status: str | None = None
    site_published: bool = False
    vk_published: bool = False
    errors: list[str] = []


class PollChiefResponse(BaseModel):
    ok: bool
    request_id: str
    processed: int
    applied: int
    items: list[PollChiefResponseItem]


@router.post("/poll-chief", response_model=PollChiefResponse)
async def poll_chief(request: Request, limit: int = 20) -> PollChiefResponse:
    rid = getattr(request.state, "request_id", uuid.uuid4().hex)
    decisions = poll_chief_inbox(limit=limit)

    applied = 0
    items: list[PollChiefResponseItem] = []
    for d in decisions:
        if not _ID_RE.match(d.publication_id):
            items.append(PollChiefResponseItem(publication_id=d.publication_id, applied=False))
            continue
        if not item_exists(d.publication_id):
            items.append(PollChiefResponseItem(publication_id=d.publication_id, applied=False))
            continue

        errs: list[str] = []
        site_pub = False
        vk_pub = False

        if d.kind == "approve":
            set_status(d.publication_id, status="approved", message_id=d.message_id)
            item = load_item(d.publication_id)

            # Publish to "site" storage + API (idempotent)
            if not item.site_published_at_utc:
                try:
                    pub_at, url_path = publish_to_site(item)
                    update_item(d.publication_id, site_published_at_utc=pub_at, site_url=url_path, last_publish_error=None)
                    site_pub = True
                except Exception as e:
                    errs.append(f"site_publish_failed: {e}")
                    update_item(d.publication_id, last_publish_error=str(e))

            # Publish to VK if configured (idempotent)
            item = load_item(d.publication_id)
            if not item.vk_published_at_utc:
                try:
                    pub_at, post_id, post_url = await publish_to_vk(item)
                    update_item(
                        d.publication_id,
                        vk_published_at_utc=pub_at,
                        vk_post_id=post_id,
                        vk_post_url=post_url,
                        last_publish_error=None,
                    )
                    vk_pub = True
                except Exception as e:
                    # If VK isn't configured, keep it as a soft failure.
                    errs.append(f"vk_publish_failed: {e}")
                    update_item(d.publication_id, last_publish_error=str(e))

            applied += 1
            items.append(
                PollChiefResponseItem(
                    publication_id=d.publication_id,
                    applied=True,
                    new_status="approved",
                    site_published=site_pub,
                    vk_published=vk_pub,
                    errors=errs,
                )
            )
        elif d.kind == "reject":
            set_status(d.publication_id, status="rejected", message_id=d.message_id)
            # Move to archive immediately; purge keeps 30 days.
            try:
                archive_item(d.publication_id)
            except Exception as e:
                errs.append(f"archive_failed: {e}")
            applied += 1
            items.append(
                PollChiefResponseItem(
                    publication_id=d.publication_id,
                    applied=True,
                    new_status="rejected",
                    errors=errs,
                )
            )
        elif d.kind == "edit":
            set_status(d.publication_id, status="needs_edit", explanation=d.explanation, message_id=d.message_id)
            applied += 1
            items.append(PollChiefResponseItem(publication_id=d.publication_id, applied=True, new_status="needs_edit", errors=errs))
        else:
            items.append(PollChiefResponseItem(publication_id=d.publication_id, applied=False))

        json_log(
            {
                "type": "chief_decision_applied",
                "request_id": rid,
                "publication_id": d.publication_id,
                "kind": d.kind,
                "message_id": d.message_id,
            }
        )

    # housekeeping: archive cleanup (matches “30 дней” из ТЗ, на уровне архива)
    purged = purge_archived_older_than_days(30)
    if purged:
        json_log({"type": "content_archive_purged", "request_id": rid, "removed": purged})

    return PollChiefResponse(ok=True, request_id=rid, processed=len(decisions), applied=applied, items=items)


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

