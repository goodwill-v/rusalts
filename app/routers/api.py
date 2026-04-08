from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app import config
from app import kb
from app.observability import json_log
from app.routerai import RouterAIError, chat_completion
from app.templates_engine import load_templates_bundle, load_triggers, match_trigger, render_template

router = APIRouter(prefix="/api", tags=["api"])

SAFE_NAME = re.compile(r"^[a-zA-Z0-9._-]+$")

_KB_ARTICLES: list[kb.KbArticle] | None = None
_TRIGGERS = None
_TEMPLATES_BUNDLE = None


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=16_000)
    user_id: str | None = None
    channel: str | None = None
    platform: str | None = None


class ChatResponse(BaseModel):
    reply: str
    request_id: str
    trigger_id: str | None = None
    template_key: str | None = None
    template_type: str | None = None
    sources: list[dict] = []
    used_llm: bool = False


@router.post("/chat", response_model=ChatResponse)
async def chat(request: Request, body: ChatRequest) -> ChatResponse:
    global _KB_ARTICLES, _TRIGGERS, _TEMPLATES_BUNDLE
    rid = getattr(request.state, "request_id", uuid.uuid4().hex)
    text = body.message.strip()

    if _KB_ARTICLES is None:
        _KB_ARTICLES = kb.load_articles(config.KB_ARTICLES_DIR)
    if _TRIGGERS is None:
        _TRIGGERS, _ = load_triggers(config.KB_TRIGGERS_PATH)
    if _TEMPLATES_BUNDLE is None:
        _TEMPLATES_BUNDLE = load_templates_bundle(config.TEMPLATES_BUNDLE_PATH)

    # 1) Templates by triggers (fast path, separate from KB)
    trig, trig_meta = match_trigger(_TRIGGERS, text)
    if trig is not None:
        rendered = render_template(_TEMPLATES_BUNDLE, trig.template_type, trig.template_key)
        if rendered and rendered.get("text"):
            json_log(
                {
                    "type": "trigger_match",
                    "request_id": rid,
                    "user_id": body.user_id,
                    "channel": body.channel,
                    "platform": body.platform,
                    "trigger_id": trig.id,
                    "template_key": trig.template_key,
                    "template_type": trig.template_type,
                    "hits": trig_meta.get("hits"),
                }
            )
            return ChatResponse(
                reply=str(rendered["text"]),
                request_id=rid,
                trigger_id=trig.id,
                template_key=trig.template_key,
                template_type=trig.template_type,
                sources=[],
                used_llm=False,
            )

    # 2) RAG-lite: search KB and answer with excerpts + sources
    kb_hits = kb.search(_KB_ARTICLES, text, limit=5)
    if kb_hits:
        lines: list[str] = []
        lines.append("Нашёл в базе знаний релевантные материалы:")
        for i, h in enumerate(kb_hits[:3], start=1):
            lines.append(f"\n{i}) {h['title']}")
            if h.get("excerpt"):
                lines.append(f"— {h['excerpt']}")
            if h.get("section_path"):
                lines.append(f"Раздел: {h['section_path']}")
            if h.get("updated_at_utc"):
                lines.append(f"Актуальность: {h['updated_at_utc']}")
            srcs = h.get("sources") or []
            if srcs:
                s0 = srcs[0]
                lines.append(f"Источник: {s0.get('title')} ({s0.get('url')})")

        reply_kb = "\n".join(lines).strip()

        # 3) Optional: RouterAI for synthesis if configured and KB is ambiguous
        used_llm = False
        final_reply = reply_kb
        try:
            system_prompt_path = config.BASE_DIR / "АЛЬТЕРНАТИВА_АЛТбот" / "ALT_sist.prompt.md"
            system_prompt = system_prompt_path.read_text(encoding="utf-8").strip() if system_prompt_path.is_file() else ""

            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Ответь на вопрос пользователя, опираясь ТОЛЬКО на выдержки из базы знаний ниже. "
                        "Если в выдержках нет ответа — так и скажи и предложи обратиться в поддержку.\n\n"
                        f"Вопрос: {text}\n\n"
                        f"Выдержки (JSON): {json.dumps(kb_hits[:5], ensure_ascii=False)}"
                    ),
                }
            )
            llm_text, usage, _raw = await chat_completion(
                base_url=config.ROUTERAI_BASE_URL,
                api_key=config.ROUTERAI_API_KEY,
                model=config.ROUTERAI_CHAT_MODEL,
                messages=messages,
            )
            used_llm = True
            final_reply = str(llm_text).strip() or reply_kb
            json_log(
                {
                    "type": "routerai_usage",
                    "request_id": rid,
                    "user_id": body.user_id,
                    "channel": body.channel,
                    "platform": body.platform,
                    "model": usage.model,
                    "tokens_in": usage.input_tokens,
                    "tokens_out": usage.output_tokens,
                    "cost_usd": usage.cost_usd,
                    "purpose": "chat",
                }
            )
        except RouterAIError:
            # fall back silently to KB-only answer
            pass

        return ChatResponse(
            reply=final_reply,
            request_id=rid,
            sources=[
                {"title": s.get("title"), "url": s.get("url")}
                for h in kb_hits[:5]
                for s in (h.get("sources") or [])
            ],
            used_llm=used_llm,
        )

    # 4) Absolute fallback (no KB hit and no trigger)
    fallback = (
        "Я не нашёл точный ответ в базе знаний и шаблонах.\n\n"
        "Попробуйте переформулировать вопрос или напишите: «переезд» / «комплаенс» / «документ».\n"
        "Если это срочно — обратитесь в поддержку."
    )
    return ChatResponse(reply=fallback, request_id=rid, sources=[], used_llm=False)


@router.get("/kb/search")
async def kb_search(q: str, request: Request, limit: int = 5) -> dict:
    global _KB_ARTICLES
    rid = getattr(request.state, "request_id", uuid.uuid4().hex)
    if _KB_ARTICLES is None:
        _KB_ARTICLES = kb.load_articles(config.KB_ARTICLES_DIR)
    hits = kb.search(_KB_ARTICLES, q, limit=limit)
    json_log({"type": "kb_search", "request_id": rid, "q": q, "limit": limit, "hits": len(hits)})
    return {"request_id": rid, "items": hits}


@router.get("/document-templates")
async def list_document_templates() -> dict:
    config.ensure_data_dirs()
    items: list[dict] = []
    for path in sorted(config.DOCUMENT_TEMPLATES_DIR.iterdir()):
        if not path.is_file():
            continue
        if path.name.startswith(".") or path.name == "README":
            continue
        rel = f"/api/files/document-templates/{path.name}"
        items.append(
            {
                "name": path.stem.replace("_", " "),
                "filename": path.name,
                "size": path.stat().st_size,
                "url": rel,
            }
        )
    return {"items": items}


@router.get("/files/document-templates/{filename}")
async def download_template(filename: str) -> FileResponse:
    if not SAFE_NAME.match(filename):
        raise HTTPException(status_code=400, detail="Недопустимое имя файла")
    base = config.DOCUMENT_TEMPLATES_DIR.resolve()
    path = (base / filename).resolve()
    if not path.is_file() or not path.is_relative_to(base):
        raise HTTPException(status_code=404, detail="Файл не найден")
    return FileResponse(path, filename=filename)


@router.post("/uploads")
async def upload_attachment(file: UploadFile = File(...)) -> dict:
    """Временная загрузка вложений для проверки UI (позже — в S3/БД)."""
    config.ensure_data_dirs()
    raw = await file.read()
    if len(raw) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Файл слишком большой")
    name = file.filename or "file"
    if not SAFE_NAME.match(name):
        ext = Path(name).suffix[:16] or ".bin"
        name = f"{uuid.uuid4().hex}{ext}"
    dest = (config.UPLOADS_DIR / name).resolve()
    if config.UPLOADS_DIR not in dest.parents:
        raise HTTPException(status_code=400, detail="Некорректный путь")
    dest.write_bytes(raw)
    return {"ok": True, "filename": name, "size": len(raw)}
