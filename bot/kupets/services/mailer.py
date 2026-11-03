"""Отправка писем об ошибках (SMTP)."""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage

from config import Config
from utils import get_logger

logger = get_logger("services.mailer")


@dataclass
class ErrorReport:
    user_id: int | None
    username: str | None
    full_name: str | None
    chat_id: int | None
    text: str
    last_query: str | None = None


def smtp_configured() -> bool:
    return bool(Config.SMTP_HOST and Config.SMTP_USER and Config.SMTP_PASSWORD)


async def send_error_report(report: ErrorReport) -> bool:
    """Отправляет письмо. True при успехе."""
    if not smtp_configured():
        logger.error("smtp_not_configured")
        return False
    return await asyncio.to_thread(_send_sync, report)


def _send_sync(report: ErrorReport) -> bool:
    to_addr = Config.REPORT_EMAIL_TO or "v.devops@yandex.ru"
    subject = f"[Купец] Обратная связь от пользователя {report.user_id or '?'}"
    body = (
        f"Время (UTC): {datetime.now(timezone.utc).isoformat()}\n"
        f"User ID: {report.user_id}\n"
        f"Username: @{report.username or '—'}\n"
        f"Имя: {report.full_name or '—'}\n"
        f"Chat ID: {report.chat_id}\n"
        f"Последний запрос: {report.last_query or '—'}\n"
        f"\n--- Ошибка / предложение улучшения ---\n"
        f"{report.text}\n"
    )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = Config.SMTP_FROM or Config.SMTP_USER
    msg["To"] = to_addr
    msg.set_content(body)

    try:
        if Config.SMTP_USE_SSL:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(
                Config.SMTP_HOST, Config.SMTP_PORT, context=context, timeout=30
            ) as server:
                server.login(Config.SMTP_USER, Config.SMTP_PASSWORD)
                server.send_message(msg)
        else:
            with smtplib.SMTP(Config.SMTP_HOST, Config.SMTP_PORT, timeout=30) as server:
                server.ehlo()
                server.starttls(context=ssl.create_default_context())
                server.login(Config.SMTP_USER, Config.SMTP_PASSWORD)
                server.send_message(msg)
        logger.info("error_report_sent", to=to_addr, user_id=report.user_id)
        return True
    except Exception as exc:
        logger.exception("error_report_send_failed", error=str(exc))
        return False
