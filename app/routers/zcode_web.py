"""Веб-чат с агентами ZCode на /zcode (ТЗ: /opt/alt_docs/ALT_ZAI_devops/TZ_VEB_zcode_web.md)
и таб «Алик» (ТЗ-дополнение: TZ_VEB_zcode_web_addendum_alik.md).

Зоны ВЭБ: этот роутер, app/zcode_rpc.py, app/alik_rpc.py, app/alik_store.py,
app/templates/zcode*.html, static/css/zcode_web.css, static/js/zcode_web.js,
tests/test_zcode_web.py, tests/test_alik_web.py.
zcode-runtime и openclaw-gateway НЕ трогаются (СТОП-условия обоих ТЗ).

Безопасность (ТЗ §2.1): доступ к агентам = доступ к серверу, поэтому:
- вход только по ZCODE_WEB_TOKEN из .env, rate-limit 5/мин на IP;
- cookie HttpOnly + SameSite=Lax (+ Secure на HTTPS), TTL 12 ч (или 14 дней
  при «помнить»); содержимое переписок нигде не логируется.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app import alik_store
from app.alik_rpc import AlikError, stream_chat
from app.templating import templates
from app.zcode_rpc import ZcodeRpcError, get_channel

logger = logging.getLogger("zcode_web")

router = APIRouter()

COOKIE_NAME = "zcode_web_session"
SESSION_TTL_S = 12 * 3600          # ТЗ §2.1: TTL сессии 12 ч
REMEMBER_TTL_S = 14 * 24 * 3600    # ТЗ §3.3: «помнить» — 2 недели
LOGIN_RATE_LIMIT = 5               # попыток...
LOGIN_RATE_WINDOW_S = 60           # ...в минуту с одного IP
ALIK_SEND_RATE_LIMIT = 5           # сообщений Алику... (ТЗ-дополнение §1.5 — защита gateway)
STREAM_MAX_S = 30 * 60             # SSE живёт до 30 мин, клиент переподключается
POLL_INTERVAL_S = 1.0
MAX_MESSAGE_LEN = 16000

# Рабочие пространства, чьи сессии показываем в списке (через запятую).
WORKSPACES = [
    p.strip()
    for p in os.getenv("ZCODE_WEB_WORKSPACES", "/opt/alt").split(",")
    if p.strip()
]

_login_attempts: dict[str, deque[float]] = defaultdict(deque)


def _web_token() -> str:
    return os.getenv("ZCODE_WEB_TOKEN", "").strip()


def _sign(exp: int) -> str:
    return hmac.new(_web_token().encode(), f"zcode-web|{exp}".encode(), hashlib.sha256).hexdigest()


def _make_cookie_value(ttl_s: int) -> str:
    exp = int(time.time()) + ttl_s
    return f"{exp}.{_sign(exp)}"


def _cookie_valid(value: str) -> bool:
    try:
        exp_raw, sign = value.split(".", 1)
        exp = int(exp_raw)
    except ValueError:
        return False
    if exp < int(time.time()):
        return False
    return hmac.compare_digest(sign, _sign(exp))


def _is_authed(request: Request) -> bool:
    if not _web_token():
        return False
    return _cookie_valid(request.cookies.get(COOKIE_NAME, ""))


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "?"


def _secure_cookie(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"


def _no_index(response: Response) -> Response:
    # ТЗ §2.1: noindex и запрет кеширования на всех ответах /zcode.
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Cache-Control"] = "no-store"
    return response


def _rate_limited(ip: str) -> bool:
    now = time.monotonic()
    bucket = _login_attempts[ip]
    while bucket and now - bucket[0] > LOGIN_RATE_WINDOW_S:
        bucket.popleft()
    if len(bucket) >= LOGIN_RATE_LIMIT:
        return True
    bucket.append(now)
    return False


def _err(status: int, detail: str) -> JSONResponse:
    return _no_index(JSONResponse({"detail": detail}, status_code=status))


def _session_sort_key(s: dict):
    # «Ждут вас» — важнее прочих: наверх, затем по времени активности.
    waiting_rank = 1 if s.get("status") == "waiting" else 0
    return (waiting_rank, s.get("updatedAt", 0))


def _session_public(s: dict) -> dict:
    return {
        "sessionId": s.get("sessionId", ""),
        "title": s.get("title", ""),
        "status": s.get("status", "idle"),
        "kind": s.get("sessionKind", ""),
        "workspace": (s.get("workspace") or {}).get("workspacePath", ""),
        "createdAt": s.get("createdAt", 0),
        "updatedAt": s.get("updatedAt", 0),
        "waiting": s.get("status") == "waiting",
    }


# ── Страницы и вход ──────────────────────────────────────────────────────────

@router.get("/robots.txt", include_in_schema=False)
async def robots_txt() -> Response:
    # ТЗ §2.1: /zcode закрыт от индексации.
    body = "User-agent: *\nDisallow: /zcode\n"
    return _no_index(Response(body, media_type="text/plain"))


@router.get("/zcode", response_class=Response)
async def zcode_page(request: Request):
    if not _web_token():
        # Фича не настроена: не раскрываем существование маршрута.
        return _no_index(Response("Not Found", status_code=404, media_type="text/plain"))
    if not _is_authed(request):
        return _no_index(templates.TemplateResponse(request, "zcode_login.html", {"error": ""}))
    return _no_index(templates.TemplateResponse(request, "zcode.html", {}))


@router.post("/zcode/login")
async def zcode_login(request: Request):
    if not _web_token():
        return _err(404, "Not Found")
    ip = _client_ip(request)
    if _rate_limited(ip):
        logger.warning("zcode login: rate-limited ip=%s", ip)
        return _err(429, "Слишком много попыток входа. Подождите минуту.")
    try:
        form = await request.json()
    except Exception:
        form = {}
    token = str(form.get("token", ""))
    remember = bool(form.get("remember"))
    # В лог — только факт и IP, никогда сам токен (ТЗ §2.1).
    logger.info("zcode login: attempt ip=%s ok=%s", ip, hmac.compare_digest(token, _web_token()))
    if not hmac.compare_digest(token, _web_token()):
        return _err(401, "Неверный токен.")
    ttl = REMEMBER_TTL_S if remember else SESSION_TTL_S
    response = JSONResponse({"ok": True, "ttl": ttl})
    response.set_cookie(
        COOKIE_NAME,
        _make_cookie_value(ttl),
        max_age=ttl,
        httponly=True,
        samesite="lax",
        secure=_secure_cookie(request),
        path="/",
    )
    return _no_index(response)


@router.post("/zcode/logout")
async def zcode_logout(request: Request):
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE_NAME, path="/")
    return _no_index(response)


# ── API (везде требуется cookie-аутентификация) ──────────────────────────────

def _require_auth(request: Request) -> JSONResponse | None:
    if not _web_token():
        return _err(404, "Not Found")
    if not _is_authed(request):
        return _err(401, "Требуется вход на /zcode")
    return None


@router.get("/zcode/api/sessions")
async def api_sessions(request: Request):
    denied = _require_auth(request)
    if denied:
        return denied
    channel = get_channel()
    sessions: dict[str, dict] = {}
    for workspace in WORKSPACES:
        try:
            for s in await channel.list_sessions(workspace):
                if s.get("sessionKind") not in ("interactive", "fork"):
                    continue  # субагентские/воркфлоу-сессии не показываем
                sid = s.get("sessionId")
                if sid and sid not in sessions:
                    s.setdefault("workspace", {"workspacePath": workspace})
                    sessions[sid] = s
        except ZcodeRpcError as e:
            # Рабочее пространство не запущено (existing-only) — просто пропускаем.
            logger.info("zcode sessions: workspace %s недоступен (%s)", workspace, str(e)[:80])
    items = sorted(sessions.values(), key=_session_sort_key, reverse=True)
    return _no_index(JSONResponse({"sessions": [_session_public(s) for s in items]}))


@router.get("/zcode/api/sessions/{session_id}/rows")
async def api_rows(request: Request, session_id: str, workspace: str = "",
                   before_row_id: int | None = None, limit: int = 80):
    denied = _require_auth(request)
    if denied:
        return denied
    try:
        result = await get_channel().conversation_rows(
            session_id, workspace or _default_workspace(),
            before_row_id=before_row_id, limit=limit,
        )
    except ZcodeRpcError as e:
        return _err(502, f"zcode: {str(e)[:200]}")
    rows = result.get("rows", []) if isinstance(result, dict) else []
    return _no_index(JSONResponse({
        "rows": [_row_public(r) for r in rows],
        "atSeq": result.get("atSeq") if isinstance(result, dict) else None,
    }))


@router.post("/zcode/api/sessions/{session_id}/send")
async def api_send(request: Request, session_id: str):
    denied = _require_auth(request)
    if denied:
        return denied
    try:
        body = await request.json()
    except Exception:
        return _err(400, "Ожидается JSON")
    text = str(body.get("text", "")).strip()
    workspace = str(body.get("workspace", "")).strip() or _default_workspace()
    if not text:
        return _err(400, "Пустое сообщение")
    if len(text) > MAX_MESSAGE_LEN:
        return _err(413, "Сообщение слишком длинное")
    try:
        result = await get_channel().send_prompt(session_id, workspace, text)
    except ZcodeRpcError as e:
        return _err(502, f"zcode: {str(e)[:200]}")
    accepted = bool(result.get("accepted")) if isinstance(result, dict) else False
    if not accepted:
        return _err(502, "zcode не принял сообщение")
    return _no_index(JSONResponse({"ok": True}))


@router.post("/zcode/api/sessions")
async def api_create_session(request: Request):
    denied = _require_auth(request)
    if denied:
        return denied
    try:
        body = await request.json()
    except Exception:
        body = {}
    workspace = str(body.get("workspace", "")).strip() or _default_workspace()
    try:
        result = await get_channel().create_session(workspace)
    except ZcodeRpcError as e:
        return _err(502, f"zcode: {str(e)[:200]}")
    session = (result or {}).get("session") if isinstance(result, dict) else None
    if not session:
        return _err(502, "zcode не вернул данные новой сессии")
    # API zcode имя при создании не принимает — название появится из первого
    # сообщения (ТЗ §3.1: честный отчёт об ограничении, обходов не выдумываем).
    return _no_index(JSONResponse({
        "session": _session_public(session),
        "titleFromFirstMessage": True,
    }))


def _default_workspace() -> str:
    return WORKSPACES[0] if WORKSPACES else ""


# ── SSE-стрим (поллинг v4-строк на бэкенде, JSON-события наружу) ─────────────

def _row_public(r: dict) -> dict:
    keep = {
        "kind": r.get("kind"),
        "rowId": r.get("rowId"),
    }
    if r.get("kind") == "userInput":
        keep["text"] = r.get("text", "")
    elif r.get("kind") == "assistantText":
        keep.update({"text": r.get("text", ""), "state": r.get("state", "complete")})
    elif r.get("kind") == "reasoning":
        keep.update({"text": r.get("text", ""), "state": r.get("state", "complete")})
    elif r.get("kind") == "toolCall":
        keep.update({"toolName": r.get("toolName", ""), "status": r.get("status", ""),
                     "inputText": (r.get("inputText") or "")[:400]})
    return keep


@router.get("/zcode/api/stream")
async def api_stream(request: Request, session_id: str, workspace: str = "", after_row_id: int = 0):
    denied = _require_auth(request)
    if denied:
        return denied
    ws_path = workspace or _default_workspace()

    async def event_gen():
        started = time.monotonic()
        last_row_id = after_row_id
        last_status = ""
        last_heartbeat = 0.0
        row_sigs: dict[int, str] = {}
        first_poll = True
        channel = get_channel()
        while time.monotonic() - started < STREAM_MAX_S:
            try:
                result = await channel.conversation_rows(
                    session_id, ws_path, before_row_id=None, limit=30,
                )
                rows = result.get("rows", []) if isinstance(result, dict) else []
                # Строки обновляются на месте (streaming→complete), поэтому дифим
                # по содержимому, а не только по новым rowId.
                changed = []
                for r in rows:
                    rid = r.get("rowId")
                    if not isinstance(rid, int):
                        continue
                    if rid > last_row_id:
                        last_row_id = rid
                    pub = _row_public(r)
                    sig = json.dumps(pub, sort_keys=True, ensure_ascii=False)
                    if row_sigs.get(rid) != sig:
                        row_sigs[rid] = sig
                        if not first_poll:
                            changed.append(pub)
                if len(row_sigs) > 200:  # держим окно хвостовым
                    for rid in sorted(row_sigs)[:-100]:
                        row_sigs.pop(rid, None)
                if changed:
                    payload = {"type": "rows", "rows": changed}
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                first_poll = False
                lifecycle = await channel.session_lifecycle(session_id, ws_path)
                status = str((lifecycle or {}).get("state", ""))
                if status and status != last_status:
                    last_status = status
                    payload = {"type": "status", "status": status}
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Транзиторные ошибки zcode не рвут поток: сообщаем и ждём.
                payload = {"type": "error", "message": str(e)[:160]}
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                await asyncio.sleep(3.0)
            now = time.monotonic()
            if now - last_heartbeat > 15:
                last_heartbeat = now
                yield ": ping\n\n"
            await asyncio.sleep(POLL_INTERVAL_S)

    return _no_index(StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no"},
    ))


# ── Таб «Алик» (ТЗ-дополнение: chat с OpenClaw Gateway) ──────────────────────

_alik_send_attempts: dict[str, deque[float]] = defaultdict(deque)


def _alik_rate_limited(ip: str) -> bool:
    now = time.monotonic()
    bucket = _alik_send_attempts[ip]
    while bucket and now - bucket[0] > LOGIN_RATE_WINDOW_S:
        bucket.popleft()
    if len(bucket) >= ALIK_SEND_RATE_LIMIT:
        return True
    bucket.append(now)
    return False


@router.get("/zcode/api/alik/chats")
async def alik_chats(request: Request):
    """Список чатов с Аликом — из БД (общая для телефона и десктопа)."""
    denied = _require_auth(request)
    if denied:
        return denied
    return _no_index(JSONResponse({"chats": alik_store.chats()}))


@router.post("/zcode/api/alik/chats")
async def alik_new_chat(request: Request):
    """«+ Новый чат» — новый chat_id; старые диалоги остаются в списке."""
    denied = _require_auth(request)
    if denied:
        return denied
    chat_id = alik_store.new_chat_id()
    return _no_index(JSONResponse({"chat_id": chat_id}))


@router.get("/zcode/api/alik/history")
async def alik_history(request: Request, chat_id: str, limit: int = 500):
    """История диалога из БД (F5 / другое устройство — та же история)."""
    denied = _require_auth(request)
    if denied:
        return denied
    if not chat_id.strip():
        return _err(400, "Пустой chat_id")
    return _no_index(JSONResponse({
        "chat_id": chat_id,
        "messages": alik_store.history(chat_id.strip(), limit=min(max(limit, 1), 1000)),
    }))


@router.post("/zcode/api/alik/send")
async def alik_send(request: Request):
    """Сообщение Алику: история из БД → gateway (SSE) → куски в браузер → БД.

    SSE-события: {type:"delta",text}, {type:"done"}, {type:"error",message}.
    """
    denied = _require_auth(request)
    if denied:
        return denied
    ip = _client_ip(request)
    if _alik_rate_limited(ip):
        logger.warning("alik send: rate-limited ip=%s", ip)
        return _err(429, "Слишком много сообщений. Подождите минуту.")
    try:
        body = await request.json()
    except Exception:
        return _err(400, "Ожидается JSON")
    chat_id = str(body.get("chat_id", "")).strip()
    text = str(body.get("text", "")).strip()
    if not chat_id:
        return _err(400, "Пустой chat_id")
    if not text:
        return _err(400, "Пустое сообщение")
    if len(text) > MAX_MESSAGE_LEN:
        return _err(413, "Сообщение слишком длинное")

    row_id = alik_store.add_message(chat_id, "user", text)
    messages = alik_store.context_messages(chat_id)  # последние ~30 (ТЗ §3)

    async def event_gen():
        accumulated: list[str] = []
        try:
            async for delta in stream_chat(messages, chat_id):
                accumulated.append(delta)
                payload = {"type": "delta", "text": delta}
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        except AlikError as e:
            if not accumulated:
                # Ответа не было вовсе: откатываем сообщение пользователя,
                # чтобы повторная отправка не задублировала его в истории.
                alik_store.delete_message(row_id)
            payload = {"type": "error", "message": str(e)[:220]}
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            return
        full = "".join(accumulated)
        if full.strip():
            alik_store.add_message(chat_id, "assistant", full)
        yield "data: {\"type\": \"done\"}\n\n"

    return _no_index(StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no"},
    ))
