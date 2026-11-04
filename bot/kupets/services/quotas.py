"""Квоты пользователей по тарифам (решение Шефа, 04.11 — задача Купец P1).

Гейтинг (04.11, generalize тарифов PR #71): лимит слежек берётся из плана
подписки на витрине — resource_limit из GET /api/tariffs/kupec (вместо
хардкода 5). Фолбэки (как у ВежПом, PR #75):

  - планы на витрине не заданы (сейчас так) → прежние константы
    (free 3 поиска/день без слежек; платный 6 поисков/день, 5 слежек);
  - подписка активна, код плана неизвестен → первый план продукта;
  - витрина недоступна → fail-open: платный тариф, прежние константы
    (не наказываем пользователя за наш сбой).

Поиски в день пока константой (3 free / 6 платный): в тарифных планах
витрины есть только resource_limit; отдельное поле для поисков — запрос
к Админу/Шефу (см. COORDINATION §3).

Числа квот — константы здесь (в тарифе kupec на витрине allowance_tokens=0).

Сутки квоты — календарные по МСК (Europe/Moscow): аудитория бота — РФ,
сброс в 00:00 МСК предсказуем для пользователя и совпадает с часовым
поясом суточного обхода слежки.

Счёт — «за попытку»: квота списывается при запуске поиска (до пайплайна).
Это защищает от параллельных запросов и не требует откатов при сбое.

Источник тарифа — витрина Платформы АЛТ (bot/vitrina_client.py):
GET /api/subscription/{user_id}/kupec → status=active означает и платёж,
и триал (триал живёт те же 7 дней через expires_at). Fail-open по
контракту эталонного клиента: сетевой сбой → активная подписка.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from services import sqlite_store

MOSCOW = ZoneInfo("Europe/Moscow")

FREE_PLAN = "free"
KUPEC_PLAN = "kupec"  # платный план без кода (легаси до засева планов витрины)

FREE_SEARCHES_PER_DAY = 3
PAID_SEARCHES_PER_DAY = 6
KUPEC_SEARCHES_PER_DAY = PAID_SEARCHES_PER_DAY  # алиас для совместимости (тесты/скрипты)

# Пакеты поисков — МЕСЯЧНЫЕ (корректировка Шефа 05.11, ТЗ v2 §3.0).
# Расчёт «маржа 50% при максимальном использовании»: худший случай
# ~0,9₽/поиск (2 кредита Firecrawl по PAYG $5/1000) →
# Базовый 150₽×0,5=75₽ → 80 · Продвинутый 500₽×0,5=250₽ → 270 ·
# Маркетинг 1500₽×0,5=750₽ → 800. На платном тире Firecrawl (Hobby)
# реальная себестоимость ~0,7₽/поиск — маржа выше 50%.
# Ключи — коды планов витрины; код вне словаря и легаси «kupec» (планы не
# заданы) → прежнее дневное поведение (6/день), пока Админ не засеет планы.
PLAN_SEARCHES_PER_MONTH = {
    "base": 80,       # Базовый 150₽
    "advanced": 270,  # Продвинутый 500₽
    "marketing": 800, # Маркетинг 1500₽ (для продавцов)
}
PLAN_SEARCHES_PER_DAY = {  # устаревший дневной маппинг — оставлен до пересмотра ТЗ
    "base": 6,
    "advanced": 15,
    "marketing": 40,
}
FREE_WATCH_LIMIT = 0
LEGACY_PAID_WATCH_LIMIT = 5
KUPEC_WATCH_LIMIT = LEGACY_PAID_WATCH_LIMIT  # алиас для совместимости

# Кэш планов витрины (TTL 60 с внутри клиента; здесь — последний ответ).
# Наполняется при каждом user_plan(); хендлеры всегда зовут user_plan
# ДО check_*-функций, поэтому кэш у них тёплый.
_plans_state: dict[str, Any] = {"at": 0.0, "plans": []}
_PLANS_TTL = 60.0

_client: Any | None = None


def quota_day(now: datetime | None = None) -> str:
    """Календарные сутки МСК в формате ISO-даты (2026-11-04)."""
    dt = now or datetime.now(MOSCOW)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MOSCOW)
    return dt.astimezone(MOSCOW).strftime("%Y-%m-%d")


def quota_period(plan: str, now: datetime | None = None) -> str:
    """Ключ периода счётчика поисков: «2026-11» для месячных пакетов,
    «2026-11-05» для дневных (free/легаси). Оба — календарные по МСК."""
    dt = now or datetime.now(MOSCOW)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MOSCOW)
    dt = dt.astimezone(MOSCOW)
    limits = plan_limits(plan)
    return dt.strftime("%Y-%m") if limits.get("period") == "month" else quota_day(dt)


def plan_limits(plan: str) -> dict[str, Any]:
    """Лимиты тарифа по коду плана: {searches, watches, name, period}.

    period — «month» для пакетов планов витрины (решение Шефа 05.11:
    поиски на месяц из маржи 50%) и «day» для free/легаси.
    free → 3/день; «kupec»-легаси (планы не заданы) → 6/день и 5 слежек;
    код из планов витрины → месячный пакет из PLAN_SEARCHES_PER_MONTH,
    слежки = resource_limit плана. Неизвестный код — консервативно как free
    (user_plan не возвращает неизвестных кодов).
    """
    if plan != FREE_PLAN:
        plans = _plans_state["plans"]
        for p in plans:
            if (p.get("plan") or "").strip() == plan:
                return _plan_entry_limits(p)
        if plan == KUPEC_PLAN:
            if not plans:
                return {
                    "searches": PAID_SEARCHES_PER_DAY,
                    "watches": LEGACY_PAID_WATCH_LIMIT,
                    "name": "Покупатель",
                    "period": "day",
                }
            # fail-open при засеянных планах → базовый (первый) план продукта
            return _plan_entry_limits(plans[0])
    return {
        "searches": FREE_SEARCHES_PER_DAY,
        "watches": FREE_WATCH_LIMIT,
        "name": "Бесплатный",
        "period": "day",
    }


def _plan_entry_limits(p: dict, fallback_code: str = "") -> dict[str, Any]:
    try:
        watches = int(p.get("resource_limit") or 0)
    except (TypeError, ValueError):
        watches = 0
    code = (p.get("plan") or "").strip()
    monthly = PLAN_SEARCHES_PER_MONTH.get(code)
    return {
        "searches": monthly if monthly else PAID_SEARCHES_PER_DAY,
        "period": "month" if monthly else "day",
        "watches": watches or LEGACY_PAID_WATCH_LIMIT,
        "name": p.get("name") or p.get("plan") or fallback_code or "Покупатель",
    }


def resolve_plan(subscription_active: bool) -> str:
    """Активная подписка (платёж или триал) → платный тариф, иначе free.

    Легаси-хелпер (простая развилка без планов витрины); основной путь —
    user_plan() с кодом плана.
    """
    return KUPEC_PLAN if subscription_active else FREE_PLAN


def _fetch_state(client: Any, user_id: int | str) -> tuple[dict, list[dict]]:
    """Синхронный сбор состояния витрины: подписка + планы.

    get_subscription_state различает 404 (нет подписки) и сетевой сбой
    (fail-open). Для старых duck-typing клиентов (фейки тестов) — фолбэк
    на check_subscription.
    """
    get_state = getattr(client, "get_subscription_state", None)
    if get_state is not None:
        try:
            sub = get_state(str(user_id)) or {"present": None}
        except Exception:
            sub = {"present": None}
    else:
        try:
            sub = {
                "present": bool(client.check_subscription(str(user_id))),
                "status": "active",
            }
        except Exception:
            sub = {"present": None}

    plans: list[dict] = []
    get_plans = getattr(client, "get_plans", None)
    if get_plans is not None:
        try:
            plans = get_plans() or []
        except Exception:
            plans = []
    return sub, plans


def _resolve_code(sub: dict, plans: list[dict]) -> str:
    """Код тарифа подписки.

    present=None (сетевой сбой) → fail-open платный (не наказываем
    пользователя за наш сбой); present=False (404) → free; не активна →
    free; активна → её план / первый план продукта / легаси-платный.
    """
    present = sub.get("present")
    if present is None:
        return KUPEC_PLAN
    if not present or (sub.get("status") or "").lower() != "active":
        return FREE_PLAN
    code = (sub.get("plan") or "").strip()
    if plans:
        if code and any((p.get("plan") or "").strip() == code for p in plans):
            return code
        return (plans[0].get("plan") or "").strip()
    return KUPEC_PLAN


async def user_plan(user_id: int | str, client: Any | None = None) -> str:
    """Код тарифа пользователя (free / код плана витрины / kupec-легаси).

    Витрина в контексте asyncio — через to_thread (клиент синхронный,
    до ~15 с на ретраи недопустимо блокировать event-loop поллинга).
    """
    c = client or _get_client()
    sub, plans = await asyncio.to_thread(_fetch_state, c, user_id)
    if plans:
        _plans_state.update(at=time.monotonic(), plans=plans)
    return _resolve_code(sub, plans)


def check_search_quota(
    user_id: int | str,
    plan: str,
    *,
    now: datetime | None = None,
    path: str | None = None,
) -> tuple[bool, dict]:
    """Можно ли запустить поиск: (разрешено, info). Счётчик не трогает."""
    period = quota_period(plan, now)
    used = sqlite_store.quota_searches(str(user_id), period, path=path)
    limits = plan_limits(plan)
    limit = limits["searches"]
    return used < limit, {
        "plan": plan,
        "plan_name": limits["name"],
        "period": limits.get("period", "day"),
        "period_key": period,
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
    """Списать один поиск из квоты (дневной/месячной по тарифу)."""
    return sqlite_store.quota_increment(str(user_id), quota_period(plan, now), path=path)


def check_watch_quota(
    user_id: int | str,
    plan: str,
    *,
    path: str | None = None,
) -> tuple[bool, dict]:
    """Можно ли добавить ещё одну слежку: (разрешено, info).

    Лимит = resource_limit плана витрины (гейтинг 04.11); планы не заданы →
    легаси-константа 5.
    """
    count = sqlite_store.watch_count_active(str(user_id), path=path)
    limits = plan_limits(plan)
    limit = limits["watches"]
    return count < limit, {
        "plan": plan,
        "plan_name": limits["name"],
        "count": count,
        "limit": limit,
    }


def _get_client() -> Any:
    global _client
    if _client is None:
        from bot.vitrina_client import VitrinaClient

        _client = VitrinaClient("kupec")
    return _client
