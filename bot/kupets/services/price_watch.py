"""Ф1 «Жду скидку»: слежка за минимальной ценой по запросу.

Механика (решения Шефа 03–04.11, задача Купец P1):
- пользователь сохраняет запрос (/watch, «следи за …»), доступно на тарифе
  «Покупатель», до 5 активных слежек (services.quotas);
- раз в сутки, в спокойном окне 07:00–09:00 МСК, бот прогоняет каждую
  активную слежку через тот же пайплайн поиска, что и обычный запрос
  (кэш и кредиты Firecrawl ведутся как у обычного поиска);
- сравнивается минимум выдачи («было → стало»); сообщение пользователю —
  ТОЛЬКО при изменении (анти-шум), иначе молчание;
- базлайн: первая проверка молча фиксирует «стартовую» минимальную цену
  (или 0 = «цены в выдаче не было»); если в новой выдаче цен нет — прошлая
  базлайн-цена сохраняется, чтобы разовый пустой поиск не притворился
  «ценой по ссылке» на следующий день;
- слежки пользователей без активной подписки (free) молча пропускаются —
  «БЕЗ слежки» на free (решение Шефа); слежка не удаляется и возобновляется
  при возврате подписки.

Кредиты: слежка тратит кредиты Firecrawl как обычный поиск (учёт и алерты
80/95/100% — в services.credit_monitor). При исчерпании месячного лимита
обход прерывается молча (алерты Шефу шлёт credit_monitor), пользователю
ничего не отправляется; отметка «день обработан» не ставится — обход
повторится в следующем тике планировщика, когда лимит пополнится.

Планировщик (watch_scheduler) — фоновая asyncio-задача внутри процесса бота
(main.py): просыпается раз в 10 минут, в окне 07:00–09:00 МСК делает один
обход в сутки (метка «сегодня уже обходили» — в SQLite, переживает рестарты).
Выбор в пользу внутрипроцессного планировщика: не нужен второй systemd-юнит,
деплоится вместе с ботом, а рестарт бота не портит логику (окно 2 часа +
метка суток в SQLite допускают и пропуск, и повторный заход).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from services import credit_monitor, quotas, sqlite_store
from utils import get_logger

logger = get_logger("services.price_watch")

MOSCOW = quotas.MOSCOW

# Спокойное окно суточного обхода: [07:00, 09:00) МСК
SWEEP_WINDOW_START_HOUR = 7
SWEEP_WINDOW_END_HOUR = 9
SWEEP_TICK_SECONDS = 600  # как часто планировщик проверяет окно

NotifyFn = Callable[[str, str], Awaitable[None]]
SearchFn = Callable[[str], Awaitable[Any]]


# ─── Детект изменения цены ───────────────────────────────────────────


def detect_price_change(
    previous: float | None, current: float | None
) -> tuple[bool, str | None]:
    """(изменилась, направление) по минимуму выдачи «было → стало».

    previous/current — в слотах базы: None = «ещё не проверяли», 0 = «в
    выдаче не было цен» (аналог «цена по ссылке»), >0 — найденный минимум.
    Направления: appeared (было «без цены», появилось число),
    down (дешевле), up (дороже). Равенство/нет данных — False.
    """
    if previous is None:
        return False, None  # первая проверка — фиксируем базлайн молча
    if current is None or current <= 0:
        return False, None  # цен в выдаче нет — сравнивать не с чем
    if previous <= 0:
        return True, "appeared"  # было «цена по ссылке» → появилась цена
    if current < previous:
        return True, "down"
    if current > previous:
        return True, "up"
    return False, None


def apply_check_result(
    previous: float | None, current: float | None
) -> tuple[bool, str | None, float | None]:
    """Итог проверки: (изменилась, направление, что писать в базлайн).

    Пустая выдача: до первой проверки пишем 0 (базлайн «цен нет»),
    после — сохраняем прошлую цену, чтобы разовый сбой не сбил базлайн.
    """
    if current is not None and current > 0:
        changed, direction = detect_price_change(previous, current)
        return changed, direction, float(current)
    if previous is None:
        return False, None, 0.0
    return False, None, None


def min_price_from_response(response: Any) -> float | None:
    """Минимум выдачи по прошедшим отбор товарам (цены 0 игнорируются)."""
    prices = [
        float(sp.product.price)
        for sp in (getattr(response, "scored", None) or [])
        if getattr(sp.product, "price", 0) and sp.product.price > 0
    ]
    return min(prices) if prices else None


def cheapest_product_from_response(response: Any) -> dict | None:
    """Самый дешёвый товар выдачи (для ссылки в уведомлении)."""
    best = None
    for sp in getattr(response, "scored", None) or []:
        p = sp.product
        if getattr(p, "price", 0) and p.price > 0:
            if best is None or p.price < best["price"]:
                best = {
                    "name": p.name or "",
                    "url": p.url or "",
                    "price": float(p.price),
                }
    return best


# ─── Формат уведомления ──────────────────────────────────────────────


def format_watch_alert(
    query: str,
    previous: float | None,
    current: float | None,
    product: dict | None = None,
) -> str:
    """«Было → стало» в стиле сообщений бота (Markdown legacy)."""
    from utils import format_price

    was_line = (
        format_price(previous)
        if previous and previous > 0
        else "цены в выдаче не было"
    )
    now_line = format_price(current or 0)
    delta = ""
    if previous and previous > 0 and current and current > 0:
        pct = (current - previous) / previous * 100
        arrow = "повышение" if pct > 0 else "скидка"
        delta = f" ({arrow} {abs(pct):.0f}%)"
    lines = [
        f"🔔 *Жду скидку: {_md(query)}*",
        "",
        f"💰 Минимальная цена изменилась: {was_line} → {now_line}{delta}",
    ]
    if product and product.get("url"):
        lines.append(f"🔗 [Самый дешёвый из найденных]({_md_url(product['url'])})")
    lines.append("")
    lines.append("_Слежка продолжается — напишу при следующем изменении._")
    return "\n".join(lines)


# ─── Суточный обход ──────────────────────────────────────────────────


async def sweep_once(
    search_fn: SearchFn,
    notify_fn: NotifyFn,
    *,
    now: datetime | None = None,
    path: str | None = None,
) -> dict:
    """Один обход всех активных слежек: поиск → минимум выдачи → «было →
    стало». Сообщение — только при изменении. Итог — статистика.

    search_fn(query) — тот же пайплайн, что у обычного запроса
    (SearchPipeline().run). notify_fn(chat_id, text) — отправка сообщения.
    """
    day = quotas.quota_day(now)
    if sqlite_store.sweep_is_done(day, path=path):
        return {"status": "already_done", "day": day}

    # Месячный лимит Firecrawl исчерпан: молча ждём (алерты Шефу уже
    # разослал credit_monitor), день НЕ помечаем — вернёмся в окне.
    if credit_monitor.usage_snapshot()["level"] == "exhausted":
        logger.warning("watch_sweep_skipped_credits_exhausted")
        return {"status": "skipped_credits", "day": day}

    watches = sqlite_store.watch_all_active(path=path)
    stats = {"checked": 0, "notified": 0, "errors": 0, "skipped_free": 0, "day": day}

    # free-пользователи слежку не получают («БЕЗ слежки», решение Шефа):
    # их слежки остаются активными, но молча пропускаются (без удаления —
    # при возврате подписки обход продолжится). План спрашиваем у витрины
    # один раз на пользователя за обход.
    plan_cache: dict[str, str] = {}

    async def plan_of(user_id: str) -> str:
        if user_id not in plan_cache:
            plan_cache[user_id] = await quotas.user_plan(user_id)
        return plan_cache[user_id]

    for w in watches:
        try:
            if await plan_of(w["user_id"]) != quotas.KUPEC_PLAN:
                stats["skipped_free"] += 1
                continue
        except Exception as exc:
            # витрина недоступна — не теряем обход из-за неё (fail-open
            # уже внутри quotas.user_plan; сюда дойдут только чужие сбои)
            stats["errors"] += 1
            logger.warning(
                "watch_plan_check_failed",
                extra={"watch_id": w["id"], "error": str(exc)},
            )
            continue
        try:
            response = await search_fn(w["query"])
        except Exception as exc:
            stats["errors"] += 1
            logger.warning(
                "watch_search_failed", extra={"watch_id": w["id"], "error": str(exc)}
            )
            continue
        if getattr(response, "provider_limited", False):
            # Лимит кончился посреди обхода — прерываем, день не помечаем.
            logger.warning("watch_sweep_interrupted_credits_exhausted")
            return {"status": "interrupted_credits", **stats}

        previous = w["last_min_price"]
        current = min_price_from_response(response)
        changed, direction, to_store = apply_check_result(previous, current)
        sqlite_store.watch_mark_checked(
            w["id"], min_price=to_store, checked_at=time.time(), path=path
        )
        stats["checked"] += 1

        if changed:
            text = format_watch_alert(
                w["query"], previous, current, cheapest_product_from_response(response)
            )
            try:
                await notify_fn(w["chat_id"], text)
                stats["notified"] += 1
            except Exception as exc:
                logger.warning(
                    "watch_notify_failed",
                    extra={"watch_id": w["id"], "error": str(exc)},
                )
            logger.info(
                "watch_price_changed",
                extra={
                    "watch_id": w["id"],
                    "direction": direction,
                    "was": previous,
                    "now": current,
                },
            )

    sqlite_store.sweep_mark_done(day, path=path)
    logger.info("watch_sweep_done", extra=stats)
    return {"status": "done", **stats}


# ─── Планировщик ─────────────────────────────────────────────────────


def in_sweep_window(now: datetime | None = None) -> bool:
    """Сейчас внутри спокойного окна 07:00–09:00 МСК?"""
    dt = now or datetime.now(MOSCOW)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MOSCOW)
    local = dt.astimezone(MOSCOW)
    return SWEEP_WINDOW_START_HOUR <= local.hour < SWEEP_WINDOW_END_HOUR


async def watch_scheduler(
    bot: Any,
    *,
    search_fn: SearchFn,
    notify_fn: NotifyFn | None = None,
    tick_seconds: int = SWEEP_TICK_SECONDS,
) -> None:
    """Фоновый цикл бота: раз в tick_seconds проверяет окно и метку суток;
    в окне 07:00–09:00 МСК делает один суточный обход активных слежек."""
    if notify_fn is None:

        async def notify_fn(chat_id: str, text: str) -> None:
            await bot.send_message(chat_id, text, disable_web_page_preview=True)

    logger.info(
        "watch_scheduler_started",
        extra={"window": f"{SWEEP_WINDOW_START_HOUR:02d}:00-{SWEEP_WINDOW_END_HOUR:02d}:00 МСК",
               "tick_sec": tick_seconds},
    )
    while True:
        try:
            if in_sweep_window():
                await sweep_once(search_fn, notify_fn)
        except Exception as exc:
            logger.warning("watch_scheduler_tick_failed", extra={"error": str(exc)})
        await asyncio.sleep(tick_seconds)


# ─── Мелочи ──────────────────────────────────────────────────────────


def _md(text: str) -> str:
    """Минимальное экранирование Telegram Markdown (как в bot.messages)."""
    return (
        text.replace("*", "∗")
        .replace("_", "＿")
        .replace("`", "'")
        .replace("[", "(")
        .replace("]", ")")
    )


def _md_url(url: str) -> str:
    """URL внутри [текст](url): экранируем только скобки."""
    return url.replace("(", "%28").replace(")", "%29")
