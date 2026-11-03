"""Контроль лимитов провайдеров поиска и алерты Шефу.

Лимиты: Firecrawl free = 1000 кредитов/мес (Tavily — резерв, с сервера АЛТ
пока блокируется WAF). Один поиск = Config.CREDITS_COST_PER_SEARCH кредитов,
траты пишутся в SQLite (переживают рестарты).

Алерты (почта + МАКС Шефу) с анти-спамом: один уровень не чаще
Config.ALERT_COOLDOWN_HOURS часов:
  - WARN (Config.CREDITS_WARN_PCT%): «лимиты подходят к концу»;
  - CRIT (Config.CREDITS_CRIT_PCT%): «осталось мало»;
  - EXHAUSTED (HTTP 402 от Firecrawl): «лимит исчерпан, поиск отключён».

Для пользователя: user_notice() — вежливое пояснение вместо «Ничего не нашёл».
"""

from __future__ import annotations

import asyncio
import time

from config import Config
from services import sqlite_store
from utils import get_logger

logger = get_logger("services.credit_monitor")

_MESSAGE_BY_LEVEL = {
    "warn": "🟡 Купец: израсходовано {pct}% лимита Firecrawl "
            "({spent}/{limit} кредитов за {month}). Ищем второго провайдера.",
    "crit": "🟠 Купец: израсходовано {pct}% лимита Firecrawl "
            "({spent}/{limit} кредитов за {month}). До конца месяца поиск может встать.",
    "exhausted": "🔴 Купец: лимит Firecrawl ИСЧЕРПАН ({spent}/{limit} кредитов, {month}). "
                 "Поиск для пользователей отключён до пополнения тарифа. "
                 "Платный план Hobby $19/мес = 2500 поисков (решение Шефа).",
}


# ─── Учёт трат ───────────────────────────────────────────────────────


def record_search_credits(provider: str, credits: int) -> None:
    """Вызывается из parser.firecrawl_search после каждого поиска."""
    sqlite_store.credits_add(provider, credits)
    spent = sqlite_store.credits_spent(provider)
    logger.info(
        "credits_spent",
        extra={"provider": provider, "credits": credits, "month_total": spent},
    )


def usage_snapshot() -> dict:
    """Текущее состояние лимита для решений пайплайна и алертов."""
    limit = max(1, Config.FIRECRAWL_MONTHLY_CREDITS)
    spent = sqlite_store.credits_spent("firecrawl")
    pct = round(spent / limit * 100)
    if spent >= limit:
        level = "exhausted"
    elif pct >= Config.CREDITS_CRIT_PCT:
        level = "crit"
    elif pct >= Config.CREDITS_WARN_PCT:
        level = "warn"
    else:
        level = "ok"
    return {
        "provider": "firecrawl",
        "spent": spent,
        "limit": limit,
        "pct": pct,
        "level": level,
        "remaining": max(0, limit - spent),
    }


# ─── Алерты Шефу ─────────────────────────────────────────────────────


def check_and_alert() -> dict:
    """Проверить лимиты и разослать алерты (почта + МАКС). Вызывается после
    каждого поиска и при старте. Возвращает usage_snapshot()."""
    snap = usage_snapshot()
    if snap["level"] == "ok":
        return snap
    level = snap["level"]
    cooldown_sec = Config.ALERT_COOLDOWN_HOURS * 3600
    last = sqlite_store.alert_last_sent(f"credits:{level}")
    if last is not None and (time.monotonic() - last) < cooldown_sec:
        return snap  # уже оповестили недавно

    message = _MESSAGE_BY_LEVEL[level].format(
        pct=snap["pct"],
        spent=int(snap["spent"]),
        limit=snap["limit"],
        month=_month_label(),
    )
    sqlite_store.alert_mark_sent(f"credits:{level}", sent_at=time.monotonic())

    # МАКС — синхронно и быстро; почта — фоном/в потоке, чтобы не тормозить поиск
    try:
        from services.max_notify import send_max

        max_ok = send_max(message)
    except Exception as exc:
        logger.warning("alert_max_fail", extra={"error": str(exc)})
        max_ok = False

    _schedule_email(
        f"[Купец] Лимит Firecrawl: {level}",
        message + "\n\n(автоотчёт сервиса Купец)",
    )
    logger.warning(
        "credits_alert_sent",
        extra={"level": level, "pct": snap["pct"], "max_ok": max_ok},
    )
    return snap


def mark_provider_exhausted() -> None:
    """HTTP 402 от Firecrawl: немедленный алерт без порогов (кулдаун уважаем)."""
    snap = usage_snapshot()
    snap["level"] = "exhausted"
    snap["spent"] = max(snap["spent"], snap["limit"])
    snap["pct"] = 100
    cooldown_sec = Config.ALERT_COOLDOWN_HOURS * 3600
    last = sqlite_store.alert_last_sent("credits:exhausted")
    if last is not None and (time.monotonic() - last) < cooldown_sec:
        return
    message = _MESSAGE_BY_LEVEL["exhausted"].format(
        pct=100, spent=snap["spent"], limit=snap["limit"], month=_month_label()
    )
    sqlite_store.alert_mark_sent("credits:exhausted", sent_at=time.monotonic())
    try:
        from services.max_notify import send_max

        send_max(message)
    except Exception as exc:
        logger.warning("alert_max_fail", extra={"error": str(exc)})
    _schedule_email(
        "[Купец] Лимит Firecrawl исчерпан (402)",
        message + "\n\n(автоотчёт сервиса Купец)",
    )


def _schedule_email(subject: str, body: str) -> None:
    """Письмо Шефу: из работающего event-loop — фоном, иначе — в потоке."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        try:
            from services.mailer import send_service_alert

            asyncio.run(send_service_alert(subject, body))
        except Exception as exc:
            logger.warning("alert_email_fail", extra={"error": str(exc)})
        return
    try:
        from services.mailer import send_service_alert

        loop.create_task(send_service_alert(subject, body))
    except Exception as exc:
        logger.warning("alert_email_fail", extra={"error": str(exc)})


def _month_label() -> str:
    MONTHS_RU = (
        "январь", "февраль", "март", "апрель", "май", "июнь",
        "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
    )
    m = int(time.strftime("%m")) - 1
    return MONTHS_RU[max(0, min(m, 11))]


# ─── Пояснение пользователю ──────────────────────────────────────────


def user_notice() -> str | None:
    """Вежливое пояснение пользователю, если лимит на исходе/исчерпан.
    None — когда всё в порядке и пользователь ничего не должен заметить."""
    snap = usage_snapshot()
    if snap["level"] == "exhausted":
        return (
            "🛠 Поиск временно приостановлен: у сервиса закончился месячный лимит "
            "запросов к поисковым системам.\n\n"
            "Мы уже знаем об этом и включаем расширенный тариф — обычно на пару часов. "
            "Загляните позже или напишите /start, чтобы проверить."
        )
    # warn/crit: сервис работает, пользователя не тревожим — алерты уходят Шефу.
    return None
