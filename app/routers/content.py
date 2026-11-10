from __future__ import annotations

import json
import re
import secrets
import uuid
from datetime import date
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import PlainTextResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

from app import config
from app.content_excerpt import title_fallback_from_site_text
from app.markdown_plain import strip_markdown_public
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
from app.workers.content_worker import refine_corporate_item_by_id


router = APIRouter(prefix="/api/content", tags=["content"])

_ID_RE = re.compile(r"^\d{5}$")


async def _corporate_refine_bg(publication_id: str, auto_publish: bool = False) -> None:
    """Фоновый LLM-refine после «Сохранить», чтобы запрос не упирался в nginx 504.
    auto_publish (тумблер «Режим публикаций») → сразу опубликовать после refine."""
    try:
        await refine_corporate_item_by_id(publication_id)
    except Exception as e:  # noqa: BLE001
        json_log({"type": "content_corporate_refine_bg_failed", "publication_id": publication_id, "error": str(e)})
        return
    if auto_publish:
        try:
            await approve_publication_by_id(
                request_id=f"corp-auto-{publication_id}", publication_id=publication_id
            )
            json_log({"type": "content_corporate_auto_published", "publication_id": publication_id})
        except Exception as e:  # noqa: BLE001
            json_log({"type": "content_corporate_auto_publish_failed", "publication_id": publication_id, "error": str(e)})

_basic = HTTPBasic()


def _require_admin_auth(credentials: HTTPBasicCredentials = Depends(_basic)) -> str:
    ok_user = secrets.compare_digest(credentials.username or "", "admin")
    ok_pass = secrets.compare_digest(credentials.password or "", config.ADMIN_PASSWORD)
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
    # Плюс тумблер «Автоматическая публикация» из админ-панели (/admin/).
    auto_publish = False
    try:
        from app import site_settings
        auto_publish = site_settings.get_auto_publish()
    except Exception:
        auto_publish = False

    if config.CONTENT_APPROVAL_MODE == "local_autoapprove" or auto_publish:
        set_status(pub_id, status="approved", message_id="auto")
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


async def _approve_and_publish_item(
    *, rid: str, publication_id: str,
    publish_site: bool = True, publish_vk: bool = True,
) -> ApproveResponse:
    """Общая логика кнопки «Опубликовать» в веб-очереди и мгновенной публикации с /publapprov."""
    if not _ID_RE.match(publication_id):
        raise HTTPException(status_code=400, detail="Некорректный ID")
    if not item_exists(publication_id):
        raise HTTPException(status_code=404, detail="Не найдено")
    data = await approve_publication_by_id(
        request_id=rid, publication_id=publication_id,
        publish_site=publish_site, publish_vk=publish_vk,
    )
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


@router.post(
    "/queue/{publication_id}/reprocess",
    response_model=SimpleOkResponse,
    dependencies=[Depends(_require_admin_auth)],
)
async def reprocess_queue_item(
    request: Request,
    publication_id: str,
    background_tasks: BackgroundTasks,
) -> SimpleOkResponse:
    """Запуск refine в фоне (LLM + снятие md), без ожидания — иначе 504 у nginx."""
    rid = getattr(request.state, "request_id", uuid.uuid4().hex)
    if not _ID_RE.match(publication_id):
        raise HTTPException(status_code=400, detail="Некорректный ID")
    if not item_exists(publication_id):
        raise HTTPException(status_code=404, detail="Не найдено")
    background_tasks.add_task(_corporate_refine_bg, publication_id)
    json_log({"type": "content_queue_reprocess_queued", "request_id": rid, "publication_id": publication_id})
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
async def approve(request: Request, publication_id: str, body: ChannelCancelBody | None = None) -> ApproveResponse:
    rid = getattr(request.state, "request_id", uuid.uuid4().hex)
    site_cancel = bool(body.site_cancel) if body else False
    vk_cancel = bool(body.vk_cancel) if body else False
    return await _approve_and_publish_item(
        rid=rid, publication_id=publication_id,
        publish_site=not site_cancel, publish_vk=not vk_cancel,
    )


class ApproveAllResponse(SimpleOkResponse):
    approved: int
    failed: int
    items: list[ApproveResponse]


class ChannelCancelBody(BaseModel):
    """Раздельная отмена каналов для кнопки «Опубликовать» (Шеф 11.11)."""
    site_cancel: bool = False
    vk_cancel: bool = False


class ApplyItemCommand(BaseModel):
    publication_id: str
    action: str = "publish"  # publish / approve / cancel
    pinned: bool = False
    site_text: str = ""
    vk_text: str = ""
    site_cancel: bool = False  # «Отмена» в окне Сайт (решение Шефа 11.11)
    vk_cancel: bool = False    # «Отмена» в окне ВКонтакте


class ApplyAllRequest(BaseModel):
    commands: list[ApplyItemCommand] = Field(default_factory=list, max_length=500)


class ApplyAllResponse(SimpleOkResponse):
    published: int
    approved: int
    canceled: int
    failed: int
    details: list[dict]


@router.post("/queue/apply-all", response_model=ApplyAllResponse, dependencies=[Depends(_require_admin_auth)])
async def apply_all(request: Request, body: ApplyAllRequest | None = None) -> ApplyAllResponse:
    """«Применить всё» (ТЗ Шефа 01.11): выполняет команды, проставленные в карточках
    очереди. Команда не проставлена → новость публикуется. «Одобрить» → одобрить без
    публикации. «Отмена» → новость не публикуется и удаляется (cancel+архив)."""
    rid = getattr(request.state, "request_id", uuid.uuid4().hex)
    commands: dict[str, str] = {}
    pin_set: dict[str, bool] = {}
    texts: dict[str, tuple[str, str]] = {}
    if body and body.commands:
        for c in body.commands:
            pid = str(c.publication_id or "").strip()
            if not pid:
                continue
            if c.action in ("publish", "approve", "cancel"):
                commands[pid] = c.action
            if c.pinned:
                pin_set[pid] = True
            texts[pid] = (c.site_text or "", c.vk_text or "")

    pending = list_items(statuses={"pending", "needs_edit"})
    res_published = res_approved = res_canceled = res_failed = 0
    details: list[dict] = []

    async def _do_publish(pid: str) -> dict:
        r = await approve(request, pid)
        return {"publication_id": pid, "action": "publish", "ok": bool(r.ok and not r.last_publish_error)}

    for it in pending:
        pid = it.publication_id
        action = commands.get(pid, "publish")
        entry = {"publication_id": pid, "action": action, "ok": False}
        try:
            # Правки текстов из карточек применяем до действия (модель ApplyItemCommand)
            st, vt = texts.get(pid, ("", ""))
            try:
                update_item(pid, site_text=st, vk_text=vt) if (st or vt) else None
            except Exception:
                pass
            if action == "cancel":
                set_status(pid, status="canceled", message_id="web_cancel_batch")
                archive_item(pid)
                res_canceled += 1
                entry["ok"] = True
            elif action == "approve":
                set_status(pid, status="approved", message_id="web_approve_batch")
                res_approved += 1
                entry["ok"] = True
            else:
                cmd = next((c for c in (body.commands if body else []) if str(c.publication_id) == pid), None)
                if cmd and (cmd.site_cancel or cmd.vk_cancel):
                    # Раздельная отмена каналов (Шеф 11.11): публикуются только непомеченные
                    r = await approve_publication_by_id(
                        request_id=rid, publication_id=pid,
                        publish_site=not cmd.site_cancel,
                        publish_vk=not cmd.vk_cancel,
                    )
                    entry["ok"] = bool(r.get("ok"))
                    entry["skipped"] = {
                        "site": bool(cmd.site_cancel),
                        "vk": bool(cmd.vk_cancel) or not r.get("vk_published"),
                    }
                    res_published += 1 if entry["ok"] else 0
                    res_failed += 0 if entry["ok"] else 1
                else:
                    r = await _do_publish(pid)
                    entry["ok"] = bool(r.get("ok"))
                    if entry["ok"]:
                        res_published += 1
                    else:
                        res_failed += 1
            if pin_set.get(pid):
                try:
                    update_item(pid, pinned=True)
                except Exception:
                    pass
        except Exception as e:  # noqa: BLE001
            res_failed += 1
            entry["error"] = str(e)[:200]
        details.append(entry)

    json_log({"type": "content_apply_all", "request_id": rid, "published": res_published,
              "approved": res_approved, "canceled": res_canceled, "failed": res_failed})
    return ApplyAllResponse(ok=True, request_id=rid, published=res_published,
                            approved=res_approved, canceled=res_canceled,
                            failed=res_failed, details=details)


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
async def corporate_save(
    request: Request,
    body: CorporateNewsRequest,
    background_tasks: BackgroundTasks,
) -> CorporateSaveResponse:
    """Черновик в очередь; LLM-refine в фоне (иначе nginx отдаёт 504 на длинном RouterAI)."""
    rid = getattr(request.state, "request_id", uuid.uuid4().hex)
    config.ensure_data_dirs()
    pub_id = next_publication_id()
    site_s = body.site_text.strip()
    title_e = (body.title or "").strip() or title_fallback_from_site_text(site_s)
    vk_s = (body.vk_text or "").strip()  # пусто → публикации ВК не будет (Шеф 11.11)
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
    try:
        from app import site_settings as _ss
        _auto = _ss.get_auto_publish()
    except Exception:
        _auto = False
    background_tasks.add_task(_corporate_refine_bg, pub_id, _auto)
    json_log({"type": "content_corporate_saved", "request_id": rid, "publication_id": pub_id})
    return CorporateSaveResponse(ok=True, request_id=rid, publication_id=pub_id)


@router.post("/corporate/publish", response_model=ApproveResponse, dependencies=[Depends(_require_admin_auth)])
async def corporate_publish(request: Request, body: CorporateNewsRequest) -> ApproveResponse:
    """Сразу на сайт и ВК: синхронно снимаем Markdown (без LLM), чтобы не было 504; тон ВК можно доработать в очереди."""
    rid = getattr(request.state, "request_id", uuid.uuid4().hex)
    config.ensure_data_dirs()
    site_raw = body.site_text.strip()
    vk_raw = (body.vk_text or "").strip()  # пусто → публикации ВК не будет (Шеф 11.11)
    site_s = strip_markdown_public(site_raw)
    vk_s = strip_markdown_public(vk_raw)
    title_e = (body.title or "").strip() or title_fallback_from_site_text(site_s)
    pub_id = next_publication_id()
    item = ContentItem(
        publication_id=pub_id,
        created_at_utc=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        status="pending",
        title=title_e,
        site_text=site_s,
        vk_text=vk_s,
        internal_note=("corporate_portal_publish\n" + (body.internal_note or "").strip()).strip(),
        sources=[s.strip() for s in body.sources if s and str(s).strip()],
        pinned=bool(body.pinned),
    )
    save_item(item)
    json_log({"type": "content_corporate_publish", "request_id": rid, "publication_id": pub_id})
    data = await approve_publication_by_id(
        request_id=rid, publication_id=pub_id,
        publish_vk=bool(vk_s),
    )
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

