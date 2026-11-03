# -*- coding: utf-8 -*-

from __future__ import annotations

import re
from typing import Any

import httpx

from config import Config
from models.product import SearchItem
from utils import get_logger

logger = get_logger("parser.firecrawl")

FIRECRAWL_BASE = "https://api.firecrawl.dev/v2"
SEARCH_TIMEOUT = 45
MAX_RESULTS = 36


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

    async def search_ozon(self, query, price_min=None, price_max=None, limit=20):
        search_query = _build_query(query, price_min, price_max)
        http = await self._ensure()
        logger.info("firecrawl_search", extra={"query": search_query[:100]})
        try:
            url = FIRECRAWL_BASE + "/search"
            payload = {
                "query": search_query,
                "limit": min(limit, MAX_RESULTS),
                "sources": ["web"],
            }
            resp = await http.post(url, json=payload)
            if resp.status_code != 200:
                logger.error("firecrawl_fail", extra={"status": resp.status_code})
                return []
            data = resp.json()
            return _parse_results(data.get("data", {}))
        except httpx.TimeoutException:
            logger.error("firecrawl_timeout")
            return []
        except Exception as exc:
            logger.error("firecrawl_exc", extra={"error": str(exc)})
            return []

    async def aclose(self):
        if self._http is not None:
            await self._http.aclose()
            self._http = None


def _build_query(query, price_min, price_max):
    return f"{query.strip()} site:ozon.ru"


def _parse_results(data):
    items = []
    seen = set()
    for r in data.get("web") or []:
        url = r.get("url", "")
        if not url or "ozon.ru" not in url:
            continue
        pid = _extract_product_id(url)
        if not pid or pid in seen:
            continue
        seen.add(pid)
        title = r.get("title", "").strip()
        desc = r.get("description", "")
        price = _extract_price(desc) or _extract_price(title)
        rating = _extract_rating(desc) or _extract_rating(title)
        if price and price > 0:
            item = SearchItem(
                product_id=pid,
                name=title,
                price=price,
                rating=rating,
                url=url,
            )
            items.append(item)
    logger.info("firecrawl_done", extra={"total": len(data.get("web",[])), "ozon": len(items)})
    return items


def _extract_product_id(url):
    # /product/iphone-15-1797628370/ -> 1797628370
    PAT = re.compile(r"/product/(?:.*?[/-])?(\d{6,})")
    m = PAT.search(url)
    if m:
        return m.group(1)
    PAT2 = re.compile(r"-(\d{8,})/")
    m2 = PAT2.search(url)
    if m2:
        return m2.group(1)
    return ""


def _extract_price(text):
    if not text:
        return None
    RUB = chr(8381)
    PAT = re.compile(r"(\d[\d\s]+)\s*[" + RUB + "]")
    m = PAT.search(text)
    if not m:
        PAT2 = re.compile(r"(\d[\d\s]+)\s*" + chr(1088)+chr(1091)+chr(1073))
        m = PAT2.search(text)
    if not m:
        return None
    raw = m.group(1).replace(chr(32), "").replace(chr(160), "")
    try:
        return float(raw)
    except ValueError:
        return None


def _extract_rating(text):
    if not text:
        return None
    PAT = re.compile(r"(\d+[.,]\d+)\s*\d*\s*" + chr(1086)+chr(1090)+chr(1079)+chr(1099)+chr(1074))
    m = PAT.search(text)
    if m:
        try:
            return float(m.group(1).replace(",", "."))
        except ValueError:
            return None
    return None
