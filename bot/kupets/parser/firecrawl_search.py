# -*- coding: utf-8 -*-
"""Поиск по маркетплейсам через Firecrawl /search (основной канал).

Composer API Ozon заблокирован антиботом (403 fab_chlg, см. README), поэтому
единственный живой путь — поисковые сниппеты Firecrawl по трём площадкам сразу:
`<запрос> site:ozon.ru OR site:wildberries.ru OR site:market.yandex.ru`.

Экономика (docs.firecrawl.dev/billing): /search = 2 кредита за каждые 10
результатов. Держим limit=10 → 2 кредита за поиск; каждый вызов пишем в
SQLite-счётчик (services.credit_monitor), при исчерпании лимита поднимается
ProviderQuotaError.
"""

from __future__ import annotations

import hashlib
import re

import httpx

from config import Config
from models.product import SearchItem
from utils import get_logger

logger = get_logger("parser.firecrawl")

FIRECRAWL_BASE = "https://api.firecrawl.dev/v2"
SEARCH_TIMEOUT = 45

# Три площадки одним OR-запросом (1 вызов вместо 3)
MARKETPLACES = ("ozon", "wb", "yam")
_SITE_BY_MP = {
    "ozon": "site:ozon.ru",
    "wb": "site:wildberries.ru",
    "yam": "site:market.yandex.ru",
}

# Только карточки товаров: категории/подборки («купить на OZON по низкой цене»)
# не должны попадать в выдачу
_PRODUCT_URL_PATTERNS = {
    "ozon": re.compile(r"ozon\.ru/product/", re.IGNORECASE),
    "wb": re.compile(r"wildberries\.ru/catalog/\d+/detail\.aspx", re.IGNORECASE),
    "yam": re.compile(r"market\.yandex\.ru/product--", re.IGNORECASE),
}


class ProviderQuotaError(Exception):
    """Исчерпан месячный лимит провайдера поиска (Firecrawl вернул 402)."""


class FirecrawlClient:
    def __init__(self) -> None:
        self._api_key = Config.FIRECRAWL_API_KEY
        self._headers = {"Content-Type": "application/json"}
        if self._api_key:
            self._headers["Authorization"] = "Bearer %s" % self._api_key
        self._http = None

    async def _ensure(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(SEARCH_TIMEOUT + 15),
                headers=self._headers,
            )
        return self._http

    async def search_marketplaces(
        self,
        query,
        price_min=None,
        price_max=None,
        limit=10,
        marketplaces=MARKETPLACES,
    ):
        """Один /search по всем площадкам. Возвращает список SearchItem."""
        sites = " OR ".join(_SITE_BY_MP[mp] for mp in marketplaces if mp in _SITE_BY_MP)
        search_query = f"{query.strip()} {sites}"
        http = await self._ensure()
        logger.info("firecrawl_search", extra={"query": search_query[:100]})
        credits = 0
        try:
            url = FIRECRAWL_BASE + "/search"
            payload = {
                "query": search_query,
                "limit": max(1, min(int(limit), 10)),
                "sources": ["web"],
            }
            resp = await http.post(url, json=payload)
            if resp.status_code == 402:
                logger.error("firecrawl_quota_exhausted")
                raise ProviderQuotaError("Firecrawl: месячный лимит кредитов исчерпан")
            if resp.status_code != 200:
                logger.error("firecrawl_fail", extra={"status": resp.status_code})
                return []
            data = resp.json()
            # 10 результатов и меньше = 2 кредита (округление вверх по десяткам)
            credits = 2 * -(-min(int(limit), 10) // 10)
            return _parse_results(data.get("data", {}))
        except httpx.TimeoutException:
            logger.error("firecrawl_timeout")
            return []
        except ProviderQuotaError:
            raise
        except Exception as exc:
            logger.error("firecrawl_exc", extra={"error": str(exc)})
            return []
        finally:
            if credits and not Config.DRY_RUN:
                _record_credits(credits)

    # Совместимость со старым именем (использовалось в пайплайне/тестах)
    async def search_ozon(self, query, price_min=None, price_max=None, limit=20):
        return await self.search_marketplaces(
            query, price_min=price_min, price_max=price_max, limit=limit
        )

    async def aclose(self):
        if self._http is not None:
            await self._http.aclose()
            self._http = None


def _record_credits(credits: int) -> None:
    """Учёт кредитов без циклического импорта credit_monitor."""
    try:
        from services.credit_monitor import record_search_credits

        record_search_credits("firecrawl", credits)
    except Exception as exc:  # учёт не должен ломать поиск
        logger.warning("credit_record_fail", extra={"error": str(exc)})


def _parse_results(data) -> list[SearchItem]:
    """Сниппеты → товары. Товары БЕЗ цены не отбрасываем (главный баг старого
    адаптера: у Ozon цены в сниппетах почти нет, фильтр оставлял 0 из 36).
    price=0 означает «цена по ссылке» — форматы и фильтры это учитывают."""
    items: list[SearchItem] = []
    seen: set[str] = set()
    for r in data.get("web") or []:
        url = r.get("url", "")
        mp = _marketplace_of(url)
        if not mp:
            continue
        if not _PRODUCT_URL_PATTERNS[mp].search(url):
            continue  # категория/подборка, не карточка
        if mp == "ozon" and url.rstrip("/").endswith(("reviews", "reviews/")):
            continue  # страница отзывов карточки, не сам товар
        pid = _extract_product_id(url) or hashlib.sha1(url.encode()).hexdigest()[:12]
        if pid in seen:
            continue
        seen.add(pid)
        title = r.get("title", "").strip()
        desc = r.get("description", "")
        price = _extract_price(desc) or _extract_price(title) or 0.0
        rating, reviews = _extract_rating(desc) or _extract_rating(title) or (None, None)
        items.append(
            SearchItem(
                product_id=pid,
                name=title,
                price=price,
                rating=rating,
                reviews_count=reviews,
                url=url,
                marketplace=mp,
            )
        )
    logger.info(
        "firecrawl_done",
        extra={"total": len(data.get("web", [])), "products": len(items)},
    )
    return items


def _marketplace_of(url: str) -> str | None:
    if "ozon.ru" in url:
        return "ozon"
    if "wildberries.ru" in url:
        return "wb"
    if "market.yandex.ru" in url:
        return "yam"
    return None


def _extract_product_id(url: str) -> str:
    # ozon: /product/iphone-15-1797628370/ -> 1797628370
    m = re.search(r"/product/(?:.*?[/-])?(\d{6,})", url)
    if m:
        return m.group(1)
    m = re.search(r"-(\d{8,})/", url)
    if m:
        return m.group(1)
    # wb: /catalog/12345678/detail.aspx
    m = re.search(r"/catalog/(\d+)/detail", url)
    if m:
        return m.group(1)
    # yam: /product--slug/12345678/
    m = re.search(r"/product--[^/]*?/(\d{6,})", url)
    if m:
        return m.group(1)
    return ""


def _extract_price(text):
    if not text:
        return None
    RUB = chr(8381)
    PAT = re.compile(r"(\d[\d\s]+)\s*[" + RUB + "]")
    m = PAT.search(text)
    if not m:
        PAT2 = re.compile(r"(\d[\d\s]+)\s*" + chr(1088) + chr(1091) + chr(1073))
        m = PAT2.search(text)
    if not m:
        return None
    raw = m.group(1).replace(chr(32), "").replace(chr(160), "")
    try:
        return float(raw)
    except ValueError:
        return None


def _extract_rating(text):
    """«4.7 · 1245 отзывов» → (4.7, 1245). Без оценки — None."""
    if not text:
        return None
    rv = chr(1086) + chr(1090) + chr(1079) + chr(1099) + chr(1074)
    PAT = re.compile(r"(\d+[.,]\d+)\s*[\d\s]*\s*" + rv)
    m = PAT.search(text)
    if not m:
        return None
    try:
        rating = float(m.group(1).replace(",", "."))
    except ValueError:
        return None
    reviews = None
    m2 = re.search(r"(\d[\d\s]*)\s*" + rv, text)
    if m2:
        try:
            reviews = int(m2.group(1).replace(" ", "").replace("\u202f", ""))
        except ValueError:
            reviews = None
    return rating, reviews
