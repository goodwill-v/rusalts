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
    # Приоритет: поле из витрины (Админ, 05.11) → локальный маппинг → дневной легаси
    searches_month = p.get("searches_per_month") or PLAN_SEARCHES_PER_MONTH.get(code)
    return {
        "searches": int(searches_month) if searches_month else PAID_SEARCHES_PER_DAY,
        "period": "month" if searches_month else "day",
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
    if not present:
        return FREE_PLAN
    status = (sub.get("status") or "").lower()
    # Багфикс 06.11: триальная подписка на витрине имеет status='trial',
    # а не 'active' — раньше триал считался free («Лимит 0 из 0»).
    if status not in {"active", "trial"}:
        return FREE_PLAN
    # Триал = все опции «Маркетинг» (решение Шефа 05.11); признак — от витрины
    if (
        status == "trial"
        or sub.get("trial") in (True, 1, "1", "true")
        or sub.get("is_trial") in (True, 1, "1", "true")
    ):
        return "marketing"
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


# ─── Квоты фич (матрица ТЗ v2 §3.0, корректировка 05.11 — месячные) ──
# feature → план → (лимит, период); None = безлимит. Неизвестный план → free
# (консервативно; user_plan возвращает только известные коды).
FEATURE_LIMITS: dict[str, dict[str, tuple[int | None, str]]] = {
    "xvsy": {
        "free": (0, "month"),
        "base": (10, "month"),
        "advanced": (40, "month"),
        "marketing": (150, "month"),
    },
    "check": {
        "free": (1, "day"),
        "base": (20, "month"),
        "advanced": (80, "month"),
        "marketing": (None, "month"),
    },
    "podarok": {
        "free": (0, "month"),
        "base": (5, "month"),
        "advanced": (20, "month"),
        "marketing": (60, "month"),
    },
    "report": {
        "free": (0, "month"),
        "base": (0, "month"),
        "advanced": (0, "month"),
        "marketing": (30, "month"),  # решение Шефа 06.11: было 10 (≈27₽ worst-case)
    },
}


def feature_limit(plan: str, feature: str) -> tuple[int | None, str]:
    """(лимит, период) фичи для плана; limit None = безлимит."""
    table = FEATURE_LIMITS.get(feature)
    if not table:
        return (0, "month")
    return table.get(plan, table["free"])


def check_feature_quota(
    user_id: int | str,
    plan: str,
    feature: str,
    *,
    now: datetime | None = None,
    path: str | None = None,
) -> tuple[bool, dict]:
    """Можно ли сработать фиче: (разрешено, info). Счётчик не трогает."""
    limit, period = feature_limit(plan, feature)
    if limit is None:
        return True, {"plan": plan, "feature": feature, "limit": None,
                      "used": 0, "period": period}
    dt = now or datetime.now(MOSCOW)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MOSCOW)
    dt = dt.astimezone(MOSCOW)
    period_key = (
        dt.strftime("%Y-%m") if period == "month" else dt.strftime("%Y-%m-%d")
    )
    used = sqlite_store.feature_count(str(user_id), feature, period_key, path=path)
    return used < limit, {
        "plan": plan,
        "feature": feature,
        "limit": limit,
        "used": used,
        "period": period,
        "period_key": period_key,
    }


def consume_feature(
    user_id: int | str,
    feature: str,
    plan: str,
    *,
    now: datetime | None = None,
    path: str | None = None,
) -> int:
    """+1 к счётчику фичи в её периоде. Возвращает новое значение."""
    limit, period = feature_limit(plan, feature)
    dt = now or datetime.now(MOSCOW)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MOSCOW)
    dt = dt.astimezone(MOSCOW)
    period_key = (
        dt.strftime("%Y-%m") if period == "month" else dt.strftime("%Y-%m-%d")
    )
    return sqlite_store.feature_increment(
        str(user_id), feature, period_key, path=path
    )


def _get_client() -> Any:
    global _client
    if _client is None:
        from bot.vitrina_client import VitrinaClient

        _client = VitrinaClient("kupec")
    return _client
