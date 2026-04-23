from __future__ import annotations

import secrets
from typing import Any

import httpx
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status

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


def _pick_target(target: int) -> str:
    urls = config.TALK_ALLOWED_URLS or []
    if not urls:
        raise HTTPException(status_code=500, detail="TALK_ALLOWED_URLS не настроен")
    if target < 1 or target > len(urls):
        raise HTTPException(status_code=400, detail="Некорректный target")
    return urls[target - 1]


@router.post("/relay")
async def relay(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    _require_talk_key(request)
    target = int(payload.get("target") or 1)
    url = _pick_target(target)
    text = str(payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Пустой текст")

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, json={"text": text})
        r.raise_for_status()
        data = r.json()
    # ожидаем {reply: "..."}; но вернём как есть, клиент сам покажет
    return {"ok": True, "data": data}


@router.post("/relay-file")
async def relay_file(
    request: Request,
    target: int = Form(1),
    text: str = Form(""),
    file: UploadFile | None = File(default=None),
) -> dict[str, Any]:
    _require_talk_key(request)
    url = _pick_target(int(target))
    txt = (text or "").strip()
    if not (txt or file):
        raise HTTPException(status_code=400, detail="Нужно сообщение или файл")

    files = None
    if file is not None:
        content = await file.read()
        files = {"file": (file.filename or "upload.bin", content, file.content_type or "application/octet-stream")}

    data = {"text": txt}
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(url, data=data, files=files)
        r.raise_for_status()
        out = r.json()
    return {"ok": True, "data": out}

