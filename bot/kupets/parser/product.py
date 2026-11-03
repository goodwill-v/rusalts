"""Обогащение карточки товара.

Страницы /product/ часто блокируются антиботом (403).
База — плитка поиска + specs из названия.
Опционально: Composer или Playwright (USE_BROWSER).
"""

from __future__ import annotations

from config import Config
from models.product import ProductData, SearchItem
from parser.composer import ComposerClient
from parser.search import MOCK_PRODUCTS
from parser.widgets import merge_product_payload, search_item_to_product
from utils import get_logger

logger = get_logger("parser.product")

_product_pages_blocked: bool = False


async def parse_product(
    item: SearchItem,
    client: ComposerClient | None = None,
    *,
    try_detail: bool = True,
) -> ProductData | None:
    global _product_pages_blocked

    if Config.DRY_RUN:
        for p in MOCK_PRODUCTS:
            if p.product_id == item.product_id:
                return p
        return search_item_to_product(item)

    base = search_item_to_product(item)
    if not try_detail:
        return base

    path = item.url.replace("https://www.ozon.ru", "")
    if not path.startswith("/"):
        path = f"/product/{item.product_id}/"

    # 1) Playwright, если включён
    if Config.USE_BROWSER:
        try:
            from parser.browser import browser_available, fetch_product_json

            if browser_available():
                payload = await fetch_product_json(path)
                if payload and payload.get("widgetStates"):
                    return merge_product_payload(item, payload)
        except Exception as exc:
            logger.warning("browser_enrich_fail", error=str(exc))

    if _product_pages_blocked:
        return base

    owns = client is None
    client = client or ComposerClient()
    try:
        payload = await client.fetch_page(path, attempts=1)
        if payload and payload.get("widgetStates"):
            logger.info("product_enriched", id=item.product_id)
            return merge_product_payload(item, payload)
        _product_pages_blocked = True
        logger.info("product_pages_blocked_fallback_tiles")
        return base
    except Exception as exc:
        logger.warning("product_error", id=item.product_id, error=str(exc))
        return base
    finally:
        if owns:
            await client.aclose()
