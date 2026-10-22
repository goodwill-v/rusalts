"""Уведомления в MAX о лидах/заявках (Этап B5).

API: POST https://platform-api2.max.ru/messages?chat_id=...
Авторизация: заголовок Authorization: <MAX_BOT_TOKEN>.
Сертификат НУЦ Минцифры самоподписан → verify=False (SSL_CERT_NONE).

Сбой отправки никогда не ломает основной сценарий (заявка уже сохранена в БД,
письма уходят отдельно через SMTP).
"""

from __future__ import annotations

import logging

import httpx

from app import config

logger = logging.getLogger(__name__)

MAX_API_URL = "https://platform-api2.max.ru/messages"


async def notify_max(text: str) -> bool:
    """Отправить сообщение в чат лидов. Возвращает True при успехе."""
    if not config.MAX_BOT_TOKEN or not config.MAX_LEADS_CHAT_ID:
        logger.info("MAX notify skipped (not configured)")
        return False
    try:
        async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
            resp = await client.post(
                MAX_API_URL,
                params={"chat_id": config.MAX_LEADS_CHAT_ID},
                headers={"Authorization": config.MAX_BOT_TOKEN},
                json={"text": text},
            )
            if resp.status_code >= 400:
                logger.warning(
                    "MAX notify failed: %s %s", resp.status_code, resp.text[:200]
                )
                return False
            return True
    except Exception as e:  # сеть/DNS/SSL — не роняем заявку
        logger.warning("MAX notify error: %s", e)
        return False


def format_lead_message(
    diag_id: int, kind_title: str, name: str, contact: str, summary: str
) -> str:
    return (
        f"🆕 Заявка №{diag_id} — {kind_title}\n"
        f"👤 {name}\n"
        f"📧 {contact}\n"
        f"📊 {summary}"
    )
