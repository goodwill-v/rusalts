"""Ежедневный A/B-пробник каналов поиска (решение Шефа 06.11:
«отслеживай качество выдачи, сравнивай инструменты»).

Раз в сутки (по умолчанию 06:50 МСК — перед обходом слежек, заодно
смоук-тест обоих каналов) один и тот же запрос прогоняется через
Firecrawl и Serper НАПРЯМУЮ (в обход канала, чтобы получить данные
обоих провайдеров) и пишется в журнал search_quality.

По накопленным данным credit_monitor/Кор может сравнить каналы
(sqlite_store.quality_compare) и решить, кто основной.
Стоимость: 2 запроса/сутки ≈ 60₽/мес worst-case.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from config import Config
from services import sqlite_store
from utils import get_logger

logger = get_logger("services.quality_probe")

MOSCOW = ZoneInfo("Europe/Moscow")
PROBE_DONE_TTL = 20 * 3600  # метка «сегодня уже пробили»


def probe_day(now: datetime | None = None) -> str:
    dt = now or datetime.now(MOSCOW)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MOSCOW)
    return dt.astimezone(MOSCOW).strftime("%Y-%m-%d")


def seconds_until_probe(now: datetime | None = None) -> int:
    """Секунды до ближайшего пробника (06:50 МСК)."""
    dt = now or datetime.now(MOSCOW)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MOSCOW)
    dt = dt.astimezone(MOSCOW)
    target = dt.replace(hour=6, minute=50, second=0, microsecond=0)
    if dt >= target:
        target = target.replace(day=dt.day) + __import__("datetime").timedelta(days=1)
    return max(1, int((target - dt).total_seconds()))


async def _collect(provider: str, query: str, limit: int) -> dict:
    """Прямой вызов провайдера (мимо канала) → метки качества."""
    if provider == "firecrawl":
        from parser.firecrawl_search import FirecrawlClient, _parse_results

        fc = FirecrawlClient()
        try:
            raw = await fc.search_web(query, limit=limit)
        finally:
            await fc.aclose()
        items = _parse_results({"web": raw})
    else:
        from parser.serper_search import SerperClient
        from parser.firecrawl_search import _parse_results
        from parser.search_channel import _serper_query

        sc = SerperClient()
        try:
            organic = await sc.search_organic(
                _serper_query(query, ("ozon", "wb", "yam")), num=limit
            )
        finally:
            await sc.aclose()
        items = _parse_results({"web": [
            {"url": r.get("link", ""), "title": r.get("title", ""),
             "description": r.get("snippet", "")} for r in organic
        ]})
    return {
        "cards": len(items),
        "priced": sum(1 for it in items if it.price > 0),
        "rated": sum(1 for it in items if it.rating),
    }


async def run_probe(query: str | None = None) -> dict:
    query = query or Config.QUALITY_PROBE_QUERY
    out: dict = {}
    for provider in ("firecrawl", "serper"):
        try:
            metrics = await _collect(provider, query, 10)
            sqlite_store.quality_log(provider, query, metrics["cards"],
                                     metrics["priced"], metrics["rated"])
            out[provider] = metrics
            logger.info("quality_probe_done", extra={"provider": provider, **metrics})
        except Exception as exc:
            out[provider] = {"error": str(exc)[:100]}
            logger.warning("quality_probe_fail",
                           extra={"provider": provider, "error": str(exc)[:100]})
    return out


async def quality_probe_scheduler(query: str | None = None) -> None:
    """Фоновый цикл: спит до 06:50 МСК, пробует оба канала, снова спит."""
    logger.info("quality_probe_started",
                extra={"query": Config.QUALITY_PROBE_QUERY})
    while True:
        wait = seconds_until_probe()
        logger.info("quality_probe_sleep", extra={"wait_sec": wait})
        await asyncio.sleep(max(wait, 1))
        day = probe_day()
        if sqlite_store.alert_last_sent(f"probe:{day}") is not None:
            continue  # сегодня уже пробили
        await run_probe(query)
        sqlite_store.alert_mark_sent(f"probe:{day}")
