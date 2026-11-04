"""Квоты пользователей по тарифам (решение Шефа, 04.11 — задача Купец P1).

  - free: 3 поиска/день, слежка за ценой недоступна;
  - «Покупатель» (tariff kupec, включая триал): 6 поисков/день
    + до 5 активных слежек.

Источник тарифа — витрина Платформы АЛТ (bot/vitrina_client.py, уже вшит):
GET /api/subscription/{user_id}/kupec → status=active означает и платёж,
и триал (триал живёт те же 7 дней через expires_at — на витрине различий
в статусе нет). Fail-open по контракту эталонного клиента: сбой витрины
трактуется как активная подписка («Покупатель») — не наказываем пользователя
за наш сбой.

Числа квот — константы здесь (в тарифе kupec на витрине allowance_tokens=0:
«триал 7 дней, ограничение квотами бота», см. app/billing.py).

Сутки квоты — календарные по МСК (Europe/Moscow): аудитория бота — РФ,
сброс в 00:00 МСК предсказуем для пользователя и совпадает с часовым
поясом суточного обхода слежки.

Счёт — «за попытку»: квота списывается при запуске поиска (до пайплайна).
Это защищает от параллельных запросов и не требует откатов при сбое.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from services import sqlite_store

MOSCOW = ZoneInfo("Europe/Moscow")

FREE_PLAN = "free"
KUPEC_PLAN = "kupec"

FREE_SEARCHES_PER_DAY = 3
KUPEC_SEARCHES_PER_DAY = 6
FREE_WATCH_LIMIT = 0
KUPEC_WATCH_LIMIT = 5

_PLAN_LIMITS: dict[str, dict[str, int]] = {
    FREE_PLAN: {"searches": FREE_SEARCHES_PER_DAY, "watches": FREE_WATCH_LIMIT},
    KUPEC_PLAN: {"searches": KUPEC_SEARCHES_PER_DAY, "watches": KUPEC_WATCH_LIMIT},
}

_client: Any | None = None


def quota_day(now: datetime | None = None) -> str:
    """Календарные сутки МСК в формате ISO-даты (2026-11-04)."""
    dt = now or datetime.now(MOSCOW)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MOSCOW)
    return dt.astimezone(MOSCOW).strftime("%Y-%m-%d")


def plan_limits(plan: str) -> dict[str, int]:
    """Лимиты тарифа; неизвестный тариф трактуется как free (консервативно)."""
    return _PLAN_LIMITS.get(plan, _PLAN_LIMITS[FREE_PLAN])


def resolve_plan(subscription_active: bool) -> str:
    """active (платёж или триал) → «Покупатель», иначе free."""
    return KUPEC_PLAN if subscription_active else FREE_PLAN


def _get_client() -> Any:
    global _client
    if _client is None:
        from bot.vitrina_client import VitrinaClient

        _client = VitrinaClient("kupec")
    return _client


async def user_plan(user_id: int | str, client: Any | None = None) -> str:
    """Тариф пользователя через витрину. Витрина в контексте asyncio —
    через to_thread (клиент синхронный, до ~15 с на ретраи недопустимо
    блокировать event-loop поллинга)."""
    c = client or _get_client()
    try:
        active = await asyncio.to_thread(c.check_subscription, str(user_id))
    except Exception:
        return KUPEC_PLAN  # fail-open: контракт vitrina_client
    return resolve_plan(bool(active))


def check_search_quota(
    user_id: int | str,
    plan: str,
    *,
    now: datetime | None = None,
    path: str | None = None,
) -> tuple[bool, dict]:
    """Можно ли запустить поиск: (разрешено, info). Счётчик не трогает."""
    day = quota_day(now)
    used = sqlite_store.quota_searches(str(user_id), day, path=path)
    limit = plan_limits(plan)["searches"]
    return used < limit, {
        "plan": plan,
        "day": day,
        "used": used,
        "limit": limit,
    }


def consume_search(
    user_id: int | str,
    plan: str,
    *,
    now: datetime | None = None,
    path: str | None = None,
) -> int:
    """Списать один поиск из дневной квоты. Возвращает новое значение."""
    return sqlite_store.quota_increment(str(user_id), quota_day(now), path=path)


def check_watch_quota(
    user_id: int | str,
    plan: str,
    *,
    path: str | None = None,
) -> tuple[bool, dict]:
    """Можно ли добавить ещё одну слежку: (разрешено, info)."""
    count = sqlite_store.watch_count_active(str(user_id), path=path)
    limit = plan_limits(plan)["watches"]
    return count < limit, {
        "plan": plan,
        "count": count,
        "limit": limit,
    }
