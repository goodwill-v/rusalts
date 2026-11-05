"""Канальный поиск: Firecrawl (основной) → Serper (резерв/перелив).

Решение Шефа 06.11: Firecrawl не должен быть единой точкой отказа.
Правила канала:
- Firecrawl здоров и вернул результат (даже пустой) → это ответ, Serper
  не тратится (пустая ниша — тоже валидный ответ, не повод жечь резерв);
- Firecrawl недоступен (402/квота/сеть/ошибка) → Serper;
- оба недоступны → ProviderQuotaError → пайплайн показывает пользователю
  вежливое пояснение, алерты уходят Шефу (credit_monitor).

Все потребители (пайплайн, сводка, сравнение, подарки, фото-сводка) ходят
только через этот модуль — прямые вызовы FirecrawlClient в потребителях
запрещены.
"""

from __future__ import annotations

from typing import Any

from config import Config
from models.product import SearchItem
from parser.firecrawl_search import (
    MARKETPLACES,
    FirecrawlClient,
    ProviderQuotaError,
    _parse_results,
)
from parser.serper_search import SerperClient, SerperQuotaError
from services import credit_monitor, sqlite_store
from utils import get_logger

logger = get_logger("parser.search_channel")

Marketplaces = tuple[str, ...]


def _mp_tuple(marketplaces) -> Marketplaces:
    return tuple(marketplaces) if marketplaces else MARKETPLACES


async def _firecrawl(query: str, *, limit: int, mp: Marketplaces) -> list[SearchItem]:
    fc = FirecrawlClient()
    try:
        items = await fc.search_marketplaces(query, limit=limit, marketplaces=mp)
        _log_quality("firecrawl", query, items)
        return items
    finally:
        await fc.aclose()


def _log_quality(provider: str, query: str, items: list[SearchItem]) -> None:
    """Метрики выдачи в SQLite (решение Шефа: отслеживать качество каналов)."""
    try:
        sqlite_store.quality_log(
            provider, query, len(items),
            sum(1 for it in items if it.price > 0),
            sum(1 for it in items if it.rating),
        )
    except Exception as exc:
        logger.warning("quality_log_fail", extra={"error": str(exc)})


async def _firecrawl_raw(query: str, *, limit: int, mp: Marketplaces) -> list[dict]:
    fc = FirecrawlClient()
    try:
        return await fc.search_web(query, limit=limit, marketplaces=mp)
    finally:
        await fc.aclose()


async def _serper_items(query: str, *, limit: int) -> list[SearchItem]:
    """Serper → те же SearchItem (общий парсер сниппетов)."""
    sc = SerperClient()
    try:
        organic = await sc.search_organic(query, num=limit)
    finally:
        await sc.aclose()
    mapped = [
        {
            "url": r.get("link", ""),
            "title": r.get("title", ""),
            "description": r.get("snippet", ""),
        }
        for r in organic
    ]
    items = _parse_results({"web": mapped})
    _log_quality("serper", query, items)
    return items


async def _serper_raw(query: str, *, limit: int) -> list[dict]:
    sc = SerperClient()
    try:
        organic = await sc.search_organic(query, num=limit)
    finally:
        await sc.aclose()
    return [
        {
            "url": r.get("link", ""),
            "title": r.get("title", ""),
            "description": r.get("snippet", ""),
        }
        for r in organic
    ]


async def search_marketplaces(
    query: str,
    *,
    limit: int = 10,
    marketplaces: Marketplaces | None = None,
) -> list[SearchItem]:
    """Товарные карточки ниши. Raises ProviderQuotaError — оба канала исчерпаны."""
    mp = _mp_tuple(marketplaces)
    if Config.FIRECRAWL_API_KEY:
        if credit_monitor.usage_snapshot()["level"] == "exhausted":
            logger.warning("firecrawl_exhausted_switch_serper")
        else:
            try:
                return await _firecrawl(query, limit=limit, mp=mp)
            except ProviderQuotaError:
                credit_monitor.mark_provider_exhausted()
            except Exception as exc:
                logger.warning("firecrawl_fail_switch_serper", extra={"error": str(exc)})
    if Config.SERPER_API_KEY:
        try:
            return await _serper_items(query, limit=limit)
        except SerperQuotaError as exc:
            credit_monitor.mark_provider_exhausted("serper")
            raise ProviderQuotaError(str(exc)) from exc
    # резерв не настроен — прокидываем исходную ошибку/пустоту
    raise ProviderQuotaError("Firecrawl недоступен, резервный канал не настроен")


async def search_web_raw(
    query: str,
    *,
    limit: int = 10,
    marketplaces: Marketplaces | None = None,
) -> list[dict]:
    """Сырые сниппеты (сводка: карточки + ценовые сигнатуры каталогов)."""
    mp = _mp_tuple(marketplaces)
    if Config.FIRECRAWL_API_KEY:
        if credit_monitor.usage_snapshot()["level"] == "exhausted":
            logger.warning("firecrawl_exhausted_switch_serper")
        else:
            try:
                return await _firecrawl_raw(query, limit=limit, mp=mp)
            except ProviderQuotaError:
                credit_monitor.mark_provider_exhausted()
            except Exception as exc:
                logger.warning("firecrawl_raw_fail_switch_serper", extra={"error": str(exc)})
    if Config.SERPER_API_KEY:
        try:
            return await _serper_raw(query, limit=limit)
        except SerperQuotaError as exc:
            credit_monitor.mark_provider_exhausted("serper")
            raise ProviderQuotaError(str(exc)) from exc
    raise ProviderQuotaError("Firecrawl недоступен, резервный канал не настроен")
