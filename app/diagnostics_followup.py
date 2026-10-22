"""Email-цепочка после диагностики (Этап B4): письмо 2 (кейс, +2 дня) и
письмо 3 (оффер, +5 дней). Отписка — HMAC-ссылка /diagnostics/unsubscribe/.

Цикл — фоновая задача FastAPI (каждый час). Письма только тем, кто оставил
email в анкете; каждый выпуск содержит ссылку отписки.
"""

from __future__ import annotations

import hashlib
import hmac
import logging

from app import config
from app.diagnostics_store import (
    is_unsubscribed,
    list_followup_candidates,
    set_followup_stage,
)

logger = logging.getLogger(__name__)

STAGE_CASE_SENT = 1   # письмо «кейс» отправлено
STAGE_OFFER_SENT = 2  # письмо «оффер» отправлено (цепочка завершена)

CASE_DELAY_DAYS = 2
OFFER_DELAY_DAYS = 5


def unsub_token(diag_id: int) -> str:
    secret = config.UNSUB_SECRET or "dev-insecure"
    return hmac.new(
        secret.encode(), f"unsub:{diag_id}".encode(), hashlib.sha256
    ).hexdigest()[:24]


def unsub_link(diag_id: int) -> str:
    return (
        f"https://rusalts.ru/diagnostics/unsubscribe/"
        f"?id={diag_id}&t={unsub_token(diag_id)}"
    )


def verify_unsub(diag_id: int, token: str) -> bool:
    return hmac.compare_digest(unsub_token(diag_id), (token or "").strip())


def _case_email(name: str, link: str) -> tuple[str, str]:
    subject = "АЛТ: как сократить рутину — реальный пример"
    body = "\n".join([
        f"{name}, здравствуйте!",
        "",
        "Два дня назад вы прошли диагностику АЛТ. Обещали пример пользы — держу слово.",
        "",
        "Кейс: консультант проводил(а) 2–3 часа в день, отвечая на однотипные вопросы",
        "клиентов в чатах. Мы передали первую линию ИИ-ассистенту:",
        "  • типовые вопросы закрываются мгновенно, в любое время;",
        "  • сложные — собираются в короткую сводку для человека;",
        "  • сэкономленное время — 15+ часов в неделю.",
        "",
        "Похожая задача есть почти в каждом деле. Какая Routine съедает ваше время?",
        "Расскажите консультанту — подберём инструмент под вашу ситуацию:",
        "https://rusalts.ru/consultant",
        "",
        "—", "АЛТ — Альтернативные Легальные Технологии", "https://rusalts.ru",
        "", f"Не хотите получать письма: {link}",
    ])
    return subject, body


def _offer_email(name: str, link: str) -> tuple[str, str]:
    subject = "АЛТ: ваш план автономизации — следующий шаг"
    body = "\n".join([
        f"{name}, здравствуйте!",
        "",
        "Последнее письмо цепочки — коротко о шагах, которые дают измеримый результат:",
        "  1. Разобрать процессы: что можно передать ИИ уже сейчас (по вашей диагностике).",
        "  2. Запустить пилот на 2 недели: один процесс, один инструмент, понятная метрика.",
        "  3. Посчитать экономию времени и решить — масштабировать или нет.",
        "",
        "Начать удобно с консультации: https://rusalts.ru/consultant",
        "Продукты и подписки: https://rusalts.ru/techologis/",
        "",
        "Хорошего дня и короткого рабочего дня!",
        "",
        "—", "АЛТ — Альтернативные Легальные Технологии", "https://rusalts.ru",
        "", f"Отписаться от писем: {link}",
    ])
    return subject, body


def _send(to: str, subject: str, body: str) -> bool:
    try:
        from app.chief_mail import send_email

        send_email(to=to, subject=subject, body=body)
        return True
    except Exception as e:
        logger.warning("followup email failed to=%s: %s", to, e)
        return False


def followup_tick() -> dict[str, int]:
    """Один проход цепочки. Вызывается фоновым циклом и вручную."""
    stats = {"case_sent": 0, "offer_sent": 0, "skipped": 0}

    for row in list_followup_candidates(
        stage=0, min_age_days=CASE_DELAY_DAYS
    ):
        if is_unsubscribed(row["id"]):
            set_followup_stage(row["id"], STAGE_CASE_SENT)  # не беспокоить снова
            stats["skipped"] += 1
            continue
        subject, body = _case_email(row["name"] or "Коллега", unsub_link(row["id"]))
        if _send(row["email"], subject, body):
            set_followup_stage(row["id"], STAGE_CASE_SENT)
            stats["case_sent"] += 1

    for row in list_followup_candidates(
        stage=STAGE_CASE_SENT, min_age_days=OFFER_DELAY_DAYS
    ):
        if is_unsubscribed(row["id"]):
            set_followup_stage(row["id"], STAGE_OFFER_SENT)
            stats["skipped"] += 1
            continue
        subject, body = _offer_email(row["name"] or "Коллега", unsub_link(row["id"]))
        if _send(row["email"], subject, body):
            set_followup_stage(row["id"], STAGE_OFFER_SENT)
            stats["offer_sent"] += 1

    if any(stats.values()):
        logger.info("followup tick: %s", stats)
    return stats
