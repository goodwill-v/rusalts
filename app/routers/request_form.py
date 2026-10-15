"""Форма обратной связи /request/.

- GET  /request/      — страница формы (email обязателен, каналы связи опционально)
- POST /request/      — приём заявки: валидация → сохранение в requests.db →
                        письмо на Яндекс (CHIEF_EMAIL_TO) + дубль (REQUEST_EMAIL_CC)
- GET  /request/done  — страница «спасибо» (после успешной отправки)

Все кнопки «Запросить →» сайта ведут на /request/.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import config
from app.request_mail import send_request_email

router = APIRouter(tags=["request"])

templates = Jinja2Templates(directory=str(config.BASE_DIR / "app" / "templates"))

DB_PATH = config.DATA_DIR / "requests.db"

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Желаемые каналы связи (опционально, чекбоксы)
CHANNELS = [
    ("telegram", "Telegram"),
    ("max", "MAX"),
    ("whatsapp", "WhatsApp"),
    ("email", "Email"),
    ("phone", "Телефон"),
    ("vk", "ВКонтакте"),
]


def _connect() -> sqlite3.Connection:
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute(
        """CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            email TEXT NOT NULL,
            channels TEXT NOT NULL DEFAULT '',
            message TEXT NOT NULL DEFAULT '',
            page_ref TEXT NOT NULL DEFAULT '',
            ip TEXT NOT NULL DEFAULT ''
        )"""
    )
    return db


def save_request(data: dict[str, Any]) -> int:
    with closing(_connect()) as db:
        cur = db.execute(
            """INSERT INTO requests (created_at, name, email, channels, message, page_ref, ip)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                data["created_at"],
                data.get("name", "").strip(),
                data.get("email", "").strip().lower(),
                data.get("channels", "").strip(),
                data.get("message", "").strip(),
                data.get("page_ref", "").strip(),
                data.get("ip", "").strip(),
            ),
        )
        db.commit()
        return int(cur.lastrowid)


@router.get("/request/", response_class=HTMLResponse)
async def request_page(request: Request, sent: str = "") -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "request_form.html",
        {
            "vk_app_id": config.VK_APP_ID or None,
            "is_widget": False,
            "layout_class": "layout-site",
            "page_title": "Запросить — АЛЬТЕРНАТИВА (АЛТ)",
            "channels": CHANNELS,
            "sent": sent == "1",
        },
    )


@router.get("/request/done", response_class=HTMLResponse)
async def request_done(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "request_form.html",
        {
            "vk_app_id": config.VK_APP_ID or None,
            "is_widget": False,
            "layout_class": "layout-site",
            "page_title": "Заявка отправлена — АЛЬТЕРНАТИВА (АЛТ)",
            "channels": CHANNELS,
            "sent": True,
        },
    )


@router.post("/request/", response_class=HTMLResponse)
async def request_submit(request: Request) -> HTMLResponse:
    form = await request.form()
    email = (form.get("email") or "").strip().lower()
    if not email or not EMAIL_RE.match(email):
        return templates.TemplateResponse(
            request,
            "request_form.html",
            {
                "vk_app_id": config.VK_APP_ID or None,
                "is_widget": False,
                "layout_class": "layout-site",
                "page_title": "Запросить — АЛЬТЕРНАТИВА (АЛТ)",
                "channels": CHANNELS,
                "error": "Пожалуйста, укажите корректный email — это обязательное поле.",
                "form": {
                    "name": form.get("name", ""),
                    "email": email,
                    "message": form.get("message", ""),
                },
            },
            status_code=400,
        )

    selected = [
        ch[1]
        for ch in CHANNELS
        if form.get(f"ch_{ch[0]}")
    ]
    channels = ", ".join(selected) if selected else "не указаны"

    name = (form.get("name") or "").strip()
    message = (form.get("message") or "").strip()
    page_ref = (form.get("page_ref") or "").strip()

    # Адрес клиента: берём из заголовков, если доступно
    ip = request.client.host if request.client else ""

    data = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "name": name,
        "email": email,
        "channels": channels,
        "message": message,
        "page_ref": page_ref,
        "ip": ip,
    }
    rid = save_request(data)

    subject = f"📩 Новая заявка с сайта (id={rid})"
    body = (
        f"Новая заявка через форму «Запросить →»\n"
        f"------------------------------------------\n"
        f"ID: {rid}\n"
        f"Дата: {data['created_at']} UTC\n"
        f"Имя: {name or '—'}\n"
        f"Email: {email}\n"
        f"Каналы связи: {channels}\n"
        f"Откуда: {page_ref or '—'}\n"
        f"IP: {ip}\n"
        f"\nЗадачи:\n{message or '—'}\n"
    )
    mail_error = ""
    try:
        send_request_email(subject=subject, body=body)
    except Exception as e:  # noqa: BLE001 — не роняем заявку из-за почты
        mail_error = str(e)

    return templates.TemplateResponse(
        request,
        "request_form.html",
        {
            "vk_app_id": config.VK_APP_ID or None,
            "is_widget": False,
            "layout_class": "layout-site",
            "page_title": "Заявка отправлена — АЛЬТЕРНАТИВА (АЛТ)",
            "channels": CHANNELS,
            "sent": True,
            "mail_error": mail_error,
            "request_id": rid,
        },
    )