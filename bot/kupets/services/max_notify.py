"""Уведомления Шефу в МАКС (алерты лимитов/сбоев).

Тот же контракт, что app/max_notify.py в монорепо АЛТ (send лидов):
POST https://platform-api2.max.ru/messages?chat_id=... с заголовком
Authorization: <MAX_BOT_TOKEN>. Сбой отправки никогда не ломает основной
сценарий бота — только лог.
"""

from __future__ import annotations

import httpx

from config import Config
from utils import get_logger

logger = get_logger("services.max_notify")

MAX_API_URL = "https://platform-api2.max.ru/messages"


def max_configured() -> bool:
    return bool(Config.MAX_BOT_TOKEN and Config.MAX_ALERT_CHAT_ID)


def send_max(text: str) -> bool:
    """Отправить сообщение Шефу в МАКС. True при успехе (сбой — только лог)."""
    if not max_configured():
        logger.info("max_notify_skipped", extra={"reason": "not_configured"})
        return False
    try:
        resp = httpx.post(
            MAX_API_URL,
            params={"chat_id": Config.MAX_ALERT_CHAT_ID},
            headers={"Authorization": Config.MAX_BOT_TOKEN},
            json={"text": text},
            timeout=10.0,
            verify=False,  # сертификат НУЦ Минцифры самоподписан (как в app/max_notify.py)
        )
        if resp.status_code >= 400:
            logger.warning(
                "max_notify_failed",
                extra={"status": resp.status_code, "body": resp.text[:200]},
            )
            return False
        return True
    except Exception as exc:
        logger.warning("max_notify_error", extra={"error": str(exc)})
        return False
