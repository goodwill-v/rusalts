"""Парсинг продавца. При блокировке — эвристики по плитке / FBO."""

from __future__ import annotations

from cache.memory_cache import seller_cache
from config import Config
from models.product import ProductData, SellerData
from parser.composer import ComposerClient
from parser.search import MOCK_PRODUCTS
from utils import get_logger

logger = get_logger("parser.seller")

_seller_pages_blocked = False


def _synthetic_seller(
    seller_id: str,
    seller_name: str = "",
    *,
    product: ProductData | None = None,
) -> SellerData:
    if seller_id == "ozon" or (seller_name and "ozon" in seller_name.lower()):
        return SellerData(
            seller_id="ozon",
            seller_name="Ozon",
            rating=4.9,
            orders_count=15000,
            is_verified=True,
        )

    # Эвристика: по рейтингу/отзывам товара оцениваем надёжность магазина
    rating = None
    orders = None
    if product and product.rating is not None:
        rating = round(min(product.rating * 0.95, 4.7), 2)
        if product.reviews_count:
            orders = max(product.reviews_count * 3, 10)

    return SellerData(
        seller_id=seller_id or "unknown",
        seller_name=seller_name or "Сторонний продавец",
        rating=rating,
        orders_count=orders,
        is_verified=False,
    )


async def parse_seller(
    seller_id: str,
    seller_url: str = "",
    client: ComposerClient | None = None,
    product: ProductData | None = None,
) -> SellerData | None:
    global _seller_pages_blocked

    if not seller_id or seller_id == "unknown":
        return _synthetic_seller("unknown", product=product)

    cached = seller_cache.get(seller_id)
    if cached is not None:
        return cached

    if Config.DRY_RUN:
        for p in MOCK_PRODUCTS:
            if p.seller and p.seller.seller_id == seller_id:
                seller_cache.set(seller_id, p.seller)
                return p.seller
        data = SellerData(seller_id=seller_id, seller_name=seller_id, rating=4.0)
        seller_cache.set(seller_id, data)
        return data

    if _seller_pages_blocked or seller_id == "ozon" or not seller_url:
        data = _synthetic_seller(seller_id, product=product)
        seller_cache.set(seller_id, data)
        return data

    owns = client is None
    client = client or ComposerClient()
    try:
        path = seller_url.replace("https://www.ozon.ru", "")
        payload = await client.fetch_page(path, attempts=1)
        if not payload:
            _seller_pages_blocked = True
            data = _synthetic_seller(seller_id, product=product)
            seller_cache.set(seller_id, data)
            return data

        import json
        import re

        blob = json.dumps(payload.get("widgetStates") or {}, ensure_ascii=False)
        name = seller_id
        rating = None
        m = re.search(r'"name"\s*:\s*"([^"]{2,80})"', blob)
        if m:
            name = m.group(1)
        m = re.search(r'"rating"\s*:\s*([0-9.]+)', blob)
        if m:
            rating = float(m.group(1))
        data = SellerData(
            seller_id=seller_id,
            seller_name=name,
            rating=rating,
            is_verified=seller_id == "ozon" or "ozon" in name.lower(),
        )
        seller_cache.set(seller_id, data)
        return data
    except Exception as exc:
        logger.warning("seller_error", seller_id=seller_id, error=str(exc))
        return _synthetic_seller(seller_id, product=product)
    finally:
        if owns:
            await client.aclose()


async def attach_sellers(
    products: list[ProductData],
    client: ComposerClient | None = None,
) -> list[ProductData]:
    for p in products:
        if p.seller is not None and p.seller.rating is not None:
            continue
        if p.seller_id == "ozon" or p.delivery_type == "FBO":
            p.seller = _synthetic_seller("ozon", p.seller_name, product=p)
            p.seller_name = p.seller.seller_name
            continue
        if not p.seller_id or p.seller_id == "unknown":
            p.seller = _synthetic_seller("unknown", p.seller_name, product=p)
            continue
        seller = await parse_seller(
            p.seller_id, p.seller_url, client=client, product=p
        )
        if seller:
            p.seller = seller
            p.seller_name = seller.seller_name or p.seller_name
    return products
