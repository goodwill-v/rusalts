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

from app import config
from app.request_mail import send_request_email

router = APIRouter(tags=["request"])

from app.templating import templates  # noqa: E402  — общий экземпляр с content_block

DB_PATH = config.DATA_DIR / "requests.db"

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Телефон: 10–15 цифр, допускаются +7, скобки, пробелы, дефисы
PHONE_RE = re.compile(r"^\+?\d[\d\s\-\(\)]{9,18}$")

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
            ip TEXT NOT NULL DEFAULT '',
            topic TEXT NOT NULL DEFAULT '',
            contact TEXT NOT NULL DEFAULT ''
        )"""
    )
    # Идемпотентная миграция (форма «Заявка — обращение», решение Шефа 09.11)
    cols = {r[1] for r in db.execute("PRAGMA table_info(requests)").fetchall()}
    for col in ("topic", "contact"):
        if col not in cols:
            db.execute(f"ALTER TABLE requests ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
    db.commit()
    return db


def save_request(data: dict[str, Any]) -> int:
    with closing(_connect()) as db:
        cur = db.execute(
            """INSERT INTO requests (created_at, name, email, channels, message, page_ref, ip,
                                     topic, contact)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data["created_at"],
                data.get("name", "").strip(),
                data.get("email", "").strip().lower(),
                data.get("channels", "").strip(),
                data.get("message", "").strip(),
                data.get("page_ref", "").strip(),
                data.get("ip", "").strip(),
                data.get("topic", "").strip(),
                data.get("contact", "").strip(),
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
    # Контакт обязателен: телефон ИЛИ почта (решение Шефа 09.11)
    contact = (form.get("contact") or "").strip()
    email = contact.lower() if EMAIL_RE.match(contact) else ""
    if not contact:
        return templates.TemplateResponse(
            request,
            "request_form.html",
            {
                "vk_app_id": config.VK_APP_ID or None,
                "is_widget": False,
                "layout_class": "layout-site",
                "page_title": "Заявка — обращение — АЛЬТЕРНАТИВА (АЛТ)",
                "channels": CHANNELS,
                "error": "Пожалуйста, укажите контакт — телефон или почту (обязательное поле).",
                "form": {
                    "name": form.get("name", ""),
                    "contact": contact,
                    "topic": form.get("topic", ""),
                    "message": form.get("message", ""),
                },
            },
            status_code=400,
        )
    if not EMAIL_RE.match(contact) and not PHONE_RE.match(contact):
        return templates.TemplateResponse(
            request,
            "request_form.html",
            {
                "vk_app_id": config.VK_APP_ID or None,
                "is_widget": False,
                "layout_class": "layout-site",
                "page_title": "Заявка — обращение — АЛЬТЕРНАТИВА (АЛТ)",
                "channels": CHANNELS,
                "error": "Контакт не распознан: укажите телефон (например +7 900 000-00-00) или почту.",
                "form": {
                    "name": form.get("name", ""),
                    "contact": contact,
                    "topic": form.get("topic", ""),
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
    topic = (form.get("topic") or "").strip()
    message = (form.get("message") or "").strip()
    page_ref = (form.get("page_ref") or "").strip()

    # Адрес клиента: берём из заголовков, если доступно
    ip = request.client.host if request.client else ""

    data = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "name": name,
        "email": email,  # пусто, если контакт — телефон
        "channels": channels,
        "message": message,
        "page_ref": page_ref,
        "ip": ip,
        "topic": topic,
        "contact": contact,
    }
    rid = save_request(data)

    subject = f"📩 Заявка — обращение с сайта (id={rid}){': ' + topic[:60] if topic else ''}"
    body = (
        f"Заявка — обращение через форму сайта\n"
        f"------------------------------------------\n"
        f"ID: {rid}\n"
        f"Дата: {data['created_at']} UTC\n"
        f"Имя: {name or '—'}\n"
        f"Контакт: {contact}\n"
        f"Тема: {topic or '—'}\n"
        f"Каналы связи: {channels}\n"
        f"Откуда: {page_ref or '—'}\n"
        f"IP: {ip}\n"
        f"\nОписание заявки, вопроса, предложения:\n{message or '—'}\n"
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