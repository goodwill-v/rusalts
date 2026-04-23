from __future__ import annotations

import json
import os
import time
import uuid
import secrets
from typing import Any

import httpx
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse

from app import config

router = APIRouter(prefix="/api/talk", tags=["talk"])


def _require_talk_key(request: Request) -> None:
    """
    Доступ к /talk выдаётся по ключу, который вводится пользователем на странице.
    Клиент передаёт его как:
    - Authorization: Bearer <key>
    - или X-Talk-Key: <key>
    """
    if not config.TALK_KEY:
        raise HTTPException(status_code=500, detail="TALK_KEY не настроен")
    auth = (request.headers.get("authorization") or "").strip()
    xk = (request.headers.get("x-talk-key") or "").strip()
    token = ""
    if auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1].strip()
    if not token:
        token = xk
    if not token or not secrets.compare_digest(token, config.TALK_KEY):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Неверный ключ")


def _relay_url() -> str:
    url = (config.TALK_RELAY_URL or "").strip()
    if not url:
        raise HTTPException(status_code=500, detail="TALK_RELAY_URL не настроен")
    return url


def _relay_headers() -> dict[str, str]:
    h: dict[str, str] = {}
    if config.TALK_RELAY_APP_KEY:
        h["X-App-Key"] = config.TALK_RELAY_APP_KEY
    return h


def _talk_dir() -> str:
    base = str((config.DATA_DIR / "talk").resolve())
    os.makedirs(base, exist_ok=True)
    os.makedirs(os.path.join(base, "files"), exist_ok=True)
    return base


def _inbox_path() -> str:
    return os.path.join(_talk_dir(), "inbox.jsonl")


def _files_dir() -> str:
    return os.path.join(_talk_dir(), "files")


def _require_app_token(request: Request) -> None:
    """
    Токен для приложений, которые отправляют входящие сообщения/файлы в /talk.
    Заголовок: X-Talk-App-Token: <token>
    """
    expected = config.TALK_APP_TOKEN or config.TALK_KEY
    if not expected:
        raise HTTPException(status_code=500, detail="TALK_APP_TOKEN/TALK_KEY не настроен")
    got = (request.headers.get("x-talk-app-token") or "").strip()
    if not got or not secrets.compare_digest(got, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Неверный токен приложения")


@router.get("/ping")
async def ping(request: Request) -> dict:
    _require_talk_key(request)
    return {"ok": True}


@router.post("/relay")
async def relay(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    _require_talk_key(request)
    url = _relay_url()
    text = str(payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Пустой текст")

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(url, json={"text": text}, headers=_relay_headers())
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Не удалось подключиться к боту: {e}") from None

    ct = (r.headers.get("content-type") or "").lower()
    raw = r.text
    if r.status_code >= 400:
        # пробрасываем реальный ответ upstream, чтобы было видно, что именно сломалось
        raise HTTPException(status_code=502, detail={"upstream_status": r.status_code, "upstream_body": raw[:4000], "target_url": url})
    if "application/json" in ct:
        try:
            return {"ok": True, "data": r.json()}
        except Exception:
            return {"ok": True, "data": {"reply": raw}}
    return {"ok": True, "data": {"reply": raw}}


@router.post("/relay-file")
async def relay_file(
    request: Request,
    text: str = Form(""),
    file: UploadFile | None = File(default=None),
) -> dict[str, Any]:
    _require_talk_key(request)
    url = _relay_url()
    txt = (text or "").strip()
    if not (txt or file):
        raise HTTPException(status_code=400, detail="Нужно сообщение или файл")

    files = None
    if file is not None:
        content = await file.read()
        files = {"file": (file.filename or "upload.bin", content, file.content_type or "application/octet-stream")}

    data = {"text": txt}
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(url, data=data, files=files, headers=_relay_headers())
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Не удалось подключиться к боту: {e}") from None

    ct = (r.headers.get("content-type") or "").lower()
    raw = r.text
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail={"upstream_status": r.status_code, "upstream_body": raw[:4000], "target_url": url})
    if "application/json" in ct:
        try:
            return {"ok": True, "data": r.json()}
        except Exception:
            return {"ok": True, "data": {"reply": raw}}
    return {"ok": True, "data": {"reply": raw}}


@router.get("/upstream-health")
async def upstream_health(request: Request) -> dict[str, Any]:
    """Проверка связи АЛТ -> бот (без участия UI)."""
    _require_talk_key(request)
    url = _relay_url().rstrip("/") + "/health"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, headers=_relay_headers())
            body = r.text
            ct = (r.headers.get("content-type") or "").lower()
            if "application/json" in ct:
                try:
                    return {"ok": True, "status": r.status_code, "data": r.json()}
                except Exception:
                    return {"ok": True, "status": r.status_code, "data": body[:2000]}
            return {"ok": True, "status": r.status_code, "data": body[:2000]}
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Не удалось подключиться к боту: {e}") from None


@router.post("/incoming")
async def incoming(
    request: Request,
    text: str = Form(""),
    file: UploadFile | None = File(default=None),
) -> dict[str, Any]:
    """
    Входящие сообщения/файлы ОТ стороннего приложения. Сохраняем в inbox.jsonl и файлы в data/talk/files.
    """
    _require_app_token(request)
    msg = (text or "").strip()
    if not (msg or file):
        raise HTTPException(status_code=400, detail="Нужно сообщение или файл")

    ev_id = uuid.uuid4().hex
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    file_meta = None
    if file is not None:
        content = await file.read()
        safe_name = f"{ev_id}_{(file.filename or 'upload.bin').replace('/', '_')}"
        path = os.path.join(_files_dir(), safe_name)
        with open(path, "wb") as f:
            f.write(content)
        file_meta = {
            "name": safe_name,
            "orig": file.filename or safe_name,
            "type": file.content_type or "application/octet-stream",
            "size": len(content),
            "url": f"/api/talk/file/{safe_name}",
        }

    ev = {"id": ev_id, "ts_utc": ts, "from": "app", "text": msg, "file": file_meta}
    with open(_inbox_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    return {"ok": True, "id": ev_id}


@router.get("/inbox")
async def inbox(request: Request, after: str = "") -> dict[str, Any]:
    """
    Клиент /talk опрашивает входящие события. after — последний увиденный id.
    """
    _require_talk_key(request)
    path = _inbox_path()
    if not os.path.isfile(path):
        return {"ok": True, "events": []}

    events: list[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if isinstance(ev, dict):
                events.append(ev)

    if after:
        idx = -1
        for i, ev in enumerate(events):
            if str(ev.get("id") or "") == after:
                idx = i
                break
        if idx >= 0:
            events = events[idx + 1 :]
    return {"ok": True, "events": events[-200:]}


@router.get("/file/{name}")
async def get_file(request: Request, name: str) -> FileResponse:
    _require_talk_key(request)
    base = os.path.abspath(_files_dir())
    path = os.path.abspath(os.path.join(base, name))
    if not path.startswith(base + os.sep):
        raise HTTPException(status_code=400, detail="Некорректный путь")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Не найдено")
    return FileResponse(path)

