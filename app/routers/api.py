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
from app.model_routing import backend_choice, choose_main_or_heavy, is_alt_project_topic
from app.official_sources import search_official_sources
from app.web_search import search_web_snippets
from app.templates_engine import load_templates_bundle, load_triggers, match_trigger, render_template

router = APIRouter(prefix="/api", tags=["api"])

# Разрешаем кириллицу и пробелы (у нас шаблоны часто называются по-русски),
# но запрещаем любые разделители путей и спецсимволы, чтобы исключить traversal.
SAFE_NAME = re.compile(r"^[\w .,\-()]+$", re.UNICODE)

_KB_ARTICLES: list[kb.KbArticle] | None = None
_TRIGGERS = None
_TEMPLATES_BUNDLE = None


def _load_system_prompt() -> str:
    system_prompt_path = config.BASE_DIR / "АЛЬТЕРНАТИВА_АЛТбот" / "ALT_sist.prompt.md"
    try:
        return system_prompt_path.read_text(encoding="utf-8").strip() if system_prompt_path.is_file() else ""
    except Exception:
        return ""


def _shorten_to(s: str, n: int) -> str:
    t = " ".join(str(s or "").split()).strip()
    if len(t) <= n:
        return t
    cut = t[: max(0, n - 1)]
    sp = cut.rfind(" ")
    if sp > int(n * 0.6):
        cut = cut[:sp]
    return cut.rstrip() + "…"


class _RouteDecision(BaseModel):
    intent: str  # human_handoff | document_request | template_answer | kb_question | web_question | unclear
    confidence: float = 0.0
    template_key: str | None = None
    template_type: str | None = None
    document_query: str | None = None
    kb_query: str | None = None
    web_query: str | None = None
    internal_note: str | None = None


_KW_HUMAN = (
    "поговорить",
    "созвон",
    "консультант",
    "консультация",
    "менеджер",
    "сотрудничество",
    "коммерческ",
    "купить",
    "подписк",
    "оплат",
    "внедрить",
    "заказать",
)
_KW_DOC = ("шаблон", "образец", "документ", "бланк", "скачать", "файл", "docx", "pdf", "политика", "согласие")
_KW_WEB = ("новости", "сегодня", "срок", "штраф", "актуально", "что нового", "источник", "официально", "ссылка")


async def _route_message(*, text: str, triggers: list, templates_bundle: dict | None) -> _RouteDecision:
    t = re.sub(r"\s+", " ", (text or "").strip().casefold())
    if not t:
        return _RouteDecision(intent="unclear", confidence=0.0)

    if any(k in t for k in _KW_HUMAN):
        return _RouteDecision(intent="human_handoff", confidence=0.9)

    if any(k in t for k in _KW_DOC):
        return _RouteDecision(intent="document_request", confidence=0.85, document_query=text.strip())

    # Template candidates (but only if not a "human" / "doc" request)
    try:
        trig, meta = match_trigger(triggers, text)
        if trig is not None:
            return _RouteDecision(
                intent="template_answer",
                confidence=min(0.95, 0.6 + 0.15 * float(meta.get("hits") or 1)),
                template_key=trig.template_key,
                template_type=trig.template_type,
                internal_note=f"trigger={trig.id} hits={meta.get('hits')}",
            )
    except Exception:
        pass

    # Use cheap LLM router only when available; otherwise default to KB first.
    if not config.ROUTERAI_BASE_URL or not config.ROUTERAI_API_KEY:
        return _RouteDecision(intent="kb_question", confidence=0.55, kb_query=text.strip())

    system_prompt = _load_system_prompt()
    router_messages = []
    if system_prompt:
        router_messages.append({"role": "system", "content": system_prompt})
    router_messages.append(
        {
            "role": "user",
            "content": (
                "Ты маршрутизатор запросов для консультанта (MAX/ВК/законы РФ). "
                "Верни ОДИН JSON без текста вокруг:\n"
                "{"
                "\"intent\": \"human_handoff|document_request|template_answer|kb_question|web_question|unclear\","
                "\"confidence\": 0..1,"
                "\"document_query\": \"string?\","
                "\"kb_query\": \"string?\","
                "\"web_query\": \"string?\""
                "}\n"
                "Правила:\n"
                "- human_handoff: если пользователь просит связаться с человеком/консультацию/заказ/сотрудничество.\n"
                "- document_request: если просит шаблон/образец/скачать файл.\n"
                "- kb_question: если это справочный вопрос, вероятно отвечается из базы знаний проекта.\n"
                "- web_question: если требуется актуальная инфа/штрафы/сроки/официальные разъяснения.\n"
                "- template_answer выбирай только если вопрос явно про готовый текст (приглашение/чек‑лист/сообщение).\n\n"
                f"Вопрос: {text.strip()}"
            ),
        }
    )
    model = config.ROUTERAI_CHEAP_MODEL or config.BACKEND_MODEL_MAIN or config.ROUTERAI_CHAT_MODEL
    out, _usage, _raw = await chat_completion(
        base_url=config.ROUTERAI_BASE_URL,
        api_key=config.ROUTERAI_API_KEY,
        model=model,
        messages=router_messages,
        timeout_s=12.0,
    )
    try:
        obj = json.loads(str(out).strip())
        if isinstance(obj, dict):
            return _RouteDecision(**obj)
    except Exception:
        pass

    # Fallback
    if any(k in t for k in _KW_WEB):
        return _RouteDecision(intent="web_question", confidence=0.55, web_query=text.strip())
    return _RouteDecision(intent="kb_question", confidence=0.5, kb_query=text.strip())


def _support_cta() -> str:
    return "Если хотите — обратитесь в Поддержку, чтобы решить вопрос комплексно: v.devops@yandex.ru"


def _human_handoff_reply(text: str) -> str:
    return (
        "Понял запрос.\n\n"
        "Чтобы подключить консультанта и решить задачу быстрее, уточните, пожалуйста:\n"
        "1) ваша роль (бизнес/блогер/разработчик),\n"
        "2) платформа (MAX/ВК/оба),\n"
        "3) цель (миграция, канал/сообщество, разработка, комплаенс).\n\n"
        f"{_support_cta()}\n\n"
        "Если уместно, можем предложить разработку ИИ‑инструмента под ваш процесс (контент, модерация, комплаенс‑проверки, миграция аудитории)."
    ).strip()


def _doc_reply(*, query: str) -> tuple[str, list[dict]]:
    config.ensure_data_dirs()
    files: list[dict] = []
    try:
        items = []
        for path in sorted(config.DOCUMENT_TEMPLATES_DIR.iterdir()):
            if not path.is_file():
                continue
            if path.name.startswith(".") or path.name == "README":
                continue
            items.append(
                {
                    "name": path.stem.replace("_", " "),
                    "filename": path.name,
                    "size": path.stat().st_size,
                    "url": f"/api/files/document-templates/{path.name}",
                }
            )
        q = re.sub(r"\s+", " ", (query or "").strip().casefold())
        if q:
            scored = []
            for it in items:
                name = str(it["name"]).casefold()
                fn = str(it["filename"]).casefold()
                score = 0
                for w in [x for x in re.split(r"[^a-zа-я0-9]+", q) if x]:
                    if w in name or w in fn:
                        score += 1
                scored.append((score, it))
            scored.sort(key=lambda x: x[0], reverse=True)
            items2 = [it for sc, it in scored if sc > 0][:5]
            if items2:
                items = items2
        files = items[:8]
    except Exception:
        files = []

    if not files:
        return (
            "Шаблонов документов пока нет. Добавьте файлы в `templates/document_templates/` и перезапустите сервер.",
            [],
        )

    lines = ["Подобрал шаблоны документов для скачивания:"]
    for it in files:
        lines.append(f"- {it['name']} — {it['url']}")
    lines.append("")
    lines.append(_support_cta())
    return "\n".join(lines).strip(), files

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

    # 0) Route intent BEFORE touching templates/KB/docs.
    decision = await _route_message(text=text, triggers=_TRIGGERS, templates_bundle=_TEMPLATES_BUNDLE)
    json_log(
        {
            "type": "chat_route",
            "request_id": rid,
            "user_id": body.user_id,
            "channel": body.channel,
            "platform": body.platform,
            "intent": decision.intent,
            "confidence": decision.confidence,
            "template_key": decision.template_key,
            "template_type": decision.template_type,
        }
    )

    if decision.intent == "human_handoff":
        return ChatResponse(reply=_human_handoff_reply(text), request_id=rid, sources=[], used_llm=False)

    if decision.intent == "document_request":
        reply, items = _doc_reply(query=decision.document_query or text)
        return ChatResponse(reply=reply, request_id=rid, sources=items, used_llm=False)

    if decision.intent == "template_answer" and decision.template_key and decision.template_type:
        rendered = render_template(_TEMPLATES_BUNDLE, decision.template_type, decision.template_key)
        if rendered and rendered.get("text"):
            return ChatResponse(
                reply=str(rendered["text"]),
                request_id=rid,
                template_key=decision.template_key,
                template_type=decision.template_type,
                sources=[],
                used_llm=False,
            )

    # 2) RAG-lite: search KB and answer with excerpts + sources
    kb_q = (decision.kb_query or text).strip()
    kb_hits = kb.search(_KB_ARTICLES, kb_q, limit=5)
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

        # 3) Optional: RouterAI for synthesis (policy: main → heavy on legal/low-confidence)
        used_llm = False
        final_reply = reply_kb
        try:
            system_prompt = _load_system_prompt()

            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Ответь на вопрос пользователя, опираясь ТОЛЬКО на выдержки из базы знаний ниже. "
                        "Если в выдержках нет ответа — напиши строго 'NO_ANSWER'.\n\n"
                        f"Вопрос: {kb_q}\n\n"
                        f"Выдержки (JSON): {json.dumps(kb_hits[:5], ensure_ascii=False)}"
                    ),
                }
            )

            choice = backend_choice(
                text=text,
                kb_hits_count=len(kb_hits),
                main=config.BACKEND_MODEL_MAIN or config.ROUTERAI_CHAT_MODEL,
                heavy=config.BACKEND_MODEL_HEAVY,
            )
            llm_text, usage, _raw = await chat_completion(
                base_url=config.ROUTERAI_BASE_URL,
                api_key=config.ROUTERAI_API_KEY,
                model=choice.model,
                messages=messages,
            )
            if not str(llm_text).strip() and choice.model != (config.BACKEND_MODEL_HEAVY or "").strip() and config.BACKEND_MODEL_HEAVY:
                # Validation fallback: empty answer → retry once with heavy.
                choice2 = choose_main_or_heavy(
                    main=choice.model,
                    heavy=config.BACKEND_MODEL_HEAVY,
                    escalate=True,
                    reason="backend_retry_empty",
                )
                llm_text, usage, _raw = await chat_completion(
                    base_url=config.ROUTERAI_BASE_URL,
                    api_key=config.ROUTERAI_API_KEY,
                    model=choice2.model,
                    messages=messages,
                )
            used_llm = True
            final_reply = str(llm_text).strip() or reply_kb
            if final_reply.strip() == "NO_ANSWER":
                raise RuntimeError("kb_no_answer")
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
                    "routing_reason": choice.reason,
                }
            )
        except RouterAIError:
            # fall back silently to KB-only answer
            pass
        except Exception:
            # Если синтез дал NO_ANSWER — не возвращаем «воду», уходим на веб-источники ниже.
            kb_hits = []

        if kb_hits:
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

    # 4) Web/official fallback (expanded scope): short answer <=800 chars + source URL + support CTA.
    q = (decision.web_query or text).strip()
    official = await search_official_sources(query=q)
    web_hits = await search_web_snippets(query=q) if config.WEB_SEARCH_ENABLED else []

    sources: list[dict] = [{"title": o.title, "url": o.url} for o in (official or [])]
    sources += [{"title": w.title, "url": w.url} for w in (web_hits or [])]
    sources = sources[:6]

    if sources:
        try:
            system_prompt = _load_system_prompt()
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Найди ответ по источникам ниже и дай короткий ответ до 800 знаков. "
                        "В конце обязательно оставь ссылку (URL) на первоисточник из списка. "
                        "Не выдумывай факты.\n\n"
                        f"Вопрос: {q}\n\n"
                        f"Источники (JSON): {json.dumps(sources, ensure_ascii=False)}"
                    ),
                }
            )
            choice = backend_choice(
                text=q,
                kb_hits_count=0,
                main=config.BACKEND_MODEL_MAIN or config.ROUTERAI_CHAT_MODEL,
                heavy=config.BACKEND_MODEL_HEAVY,
            )
            llm_text, usage, _raw = await chat_completion(
                base_url=config.ROUTERAI_BASE_URL,
                api_key=config.ROUTERAI_API_KEY,
                model=choice.model,
                messages=messages,
                timeout_s=30.0,
            )
            answer = _shorten_to(str(llm_text).strip(), 800)
            has_url = "http://" in answer or "https://" in answer
            if not has_url:
                # Fallback: append first source URL if model forgot it
                answer = (answer + "\n" + str(sources[0].get("url") or "")).strip()
            answer = (answer + "\n\n" + _support_cta()).strip()
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
                    "purpose": "expert_web_short_answer",
                    "routing_reason": choice.reason,
                }
            )
            return ChatResponse(reply=answer, request_id=rid, sources=sources, used_llm=True)
        except Exception:
            lines = ["Нашёл источники по теме:"]
            for s in sources[:4]:
                lines.append(f"- {s.get('title') or 'Источник'} — {s.get('url')}")
            lines.append("")
            lines.append(_support_cta())
            return ChatResponse(reply="\n".join(lines).strip(), request_id=rid, sources=sources, used_llm=False)

    # 5) Absolute fallback (no KB hit, no trigger, no official excerpts)
    fallback = (
        "Не нашёл надёжных источников по вашему вопросу.\n\n" + _support_cta()
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
