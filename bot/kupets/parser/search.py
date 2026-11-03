"""Парсинг страницы поиска Ozon (Composer API + DRY_RUN моки)."""

from __future__ import annotations

from config import Config
from models.product import ProductData, SearchItem, SellerData
from parser.composer import ComposerClient
from parser.widgets import extract_search_items
from utils import get_logger

logger = get_logger("parser.search")

MOCK_PRODUCTS: list[ProductData] = [
    ProductData(
        product_id="1001",
        name="Samsung Galaxy A36 8/128 ГБ",
        price=18990,
        old_price=21990,
        rating=4.7,
        reviews_count=1245,
        seller_name="Ozon",
        seller_id="ozon",
        seller_url="https://www.ozon.ru/seller/ozon/",
        seller=SellerData(
            seller_id="ozon",
            seller_name="Ozon",
            rating=4.9,
            orders_count=15000,
            is_verified=True,
        ),
        delivery_type="FBO",
        delivery_days=1,
        delivery_price=0,
        ram_gb=8,
        storage_gb=128,
        screen_inches=6.6,
        specs_confirmed=True,
        category="Смартфоны",
        url="https://www.ozon.ru/product/1001/",
        photos=["https://example.com/a36.jpg"],
    ),
    ProductData(
        product_id="1002",
        name="Xiaomi Redmi Note 14 8/256 ГБ",
        price=17500,
        rating=4.3,
        reviews_count=612,
        seller_name="TechStore",
        seller_id="techstore",
        seller_url="https://www.ozon.ru/seller/techstore/",
        seller=SellerData(
            seller_id="techstore",
            seller_name="TechStore",
            rating=4.6,
            orders_count=2300,
            is_verified=True,
        ),
        delivery_type="FBS",
        delivery_days=2,
        delivery_price=0,
        ram_gb=8,
        storage_gb=256,
        screen_inches=6.67,
        specs_confirmed=True,
        category="Смартфоны",
        url="https://www.ozon.ru/product/1002/",
    ),
    ProductData(
        product_id="1003",
        name="Realme C75 6/128 ГБ",
        price=14990,
        rating=3.5,
        reviews_count=89,
        seller_name="MobShop",
        seller_id="mobshop",
        seller_url="https://www.ozon.ru/seller/mobshop/",
        seller=SellerData(
            seller_id="mobshop",
            seller_name="MobShop",
            rating=4.1,
            orders_count=430,
            is_verified=False,
        ),
        delivery_type="FBS",
        delivery_days=3,
        delivery_price=199,
        ram_gb=6,
        storage_gb=128,
        screen_inches=6.7,
        specs_confirmed=True,
        category="Смартфоны",
        url="https://www.ozon.ru/product/1003/",
    ),
    ProductData(
        product_id="1004",
        name="POCO X6 Pro 12/512 ГБ",
        price=24990,
        rating=4.5,
        reviews_count=890,
        seller_name="Xiaomi Official",
        seller_id="xiaomi",
        seller_url="https://www.ozon.ru/seller/xiaomi/",
        seller=SellerData(
            seller_id="xiaomi",
            seller_name="Xiaomi Official",
            rating=4.8,
            orders_count=8000,
            is_verified=True,
        ),
        delivery_type="FBO",
        delivery_days=1,
        delivery_price=0,
        ram_gb=12,
        storage_gb=512,
        screen_inches=6.67,
        specs_confirmed=True,
        category="Смартфоны",
        url="https://www.ozon.ru/product/1004/",
    ),
    ProductData(
        product_id="1005",
        name="Noname Phone Super Deal",
        price=2990,
        old_price=19990,
        rating=None,
        reviews_count=0,
        seller_name="NewSeller",
        seller_id="new1",
        seller_url="https://www.ozon.ru/seller/new1/",
        seller=SellerData(
            seller_id="new1",
            seller_name="NewSeller",
            rating=0.5,
            orders_count=3,
            is_verified=False,
        ),
        delivery_type="rFBS",
        delivery_days=7,
        delivery_price=399,
        ram_gb=8,
        storage_gb=128,
        screen_inches=6.0,
        specs_confirmed=True,
        category="Смартфоны",
        url="https://www.ozon.ru/product/1005/",
    ),
    ProductData(
        product_id="1006",
        name="Honor X9b 8/256 ГБ",
        price=19990,
        rating=4.4,
        reviews_count=320,
        seller_name="HonorStore",
        seller_id="honor",
        seller_url="https://www.ozon.ru/seller/honor/",
        seller=SellerData(
            seller_id="honor",
            seller_name="HonorStore",
            rating=4.7,
            orders_count=1200,
            is_verified=True,
        ),
        delivery_type="FBO",
        delivery_days=1,
        delivery_price=0,
        ram_gb=8,
        storage_gb=256,
        screen_inches=6.78,
        specs_confirmed=True,
        category="Смартфоны",
        url="https://www.ozon.ru/product/1006/",
    ),
    ProductData(
        product_id="1007",
        name="Смартфон без спецификаций BlackBox",
        price=16990,
        rating=4.1,
        reviews_count=150,
        seller_name="TechStore",
        seller_id="techstore",
        seller_url="https://www.ozon.ru/seller/techstore/",
        seller=SellerData(
            seller_id="techstore",
            seller_name="TechStore",
            rating=4.6,
            orders_count=2300,
            is_verified=True,
        ),
        delivery_type="FBS",
        delivery_days=2,
        delivery_price=0,
        ram_gb=None,
        storage_gb=None,
        screen_inches=None,
        specs_confirmed=False,
        category="Смартфоны",
        url="https://www.ozon.ru/product/1007/",
    ),
    ProductData(
        product_id="1008",
        name="Infinix Hot 40 8/128 ГБ",
        price=12990,
        rating=4.0,
        reviews_count=210,
        seller_name="GadgetWorld",
        seller_id="gadget",
        seller_url="https://www.ozon.ru/seller/gadget/",
        seller=SellerData(
            seller_id="gadget",
            seller_name="GadgetWorld",
            rating=4.3,
            orders_count=900,
            is_verified=False,
        ),
        delivery_type="FBS",
        delivery_days=3,
        delivery_price=0,
        ram_gb=8,
        storage_gb=128,
        screen_inches=6.56,
        specs_confirmed=True,
        category="Смартфоны",
        url="https://www.ozon.ru/product/1008/",
    ),
]


def mock_search_items() -> list[SearchItem]:
    items: list[SearchItem] = []
    for p in MOCK_PRODUCTS:
        items.append(
            SearchItem(
                product_id=p.product_id,
                name=p.name,
                price=p.price,
                old_price=p.old_price,
                rating=p.rating,
                reviews_count=p.reviews_count,
                url=p.url,
                seller_name=p.seller_name,
                seller_id=p.seller_id,
                has_fbo=p.delivery_type == "FBO",
                delivery_text="Завтра" if p.delivery_days == 1 else None,
            )
        )
    return items


async def parse_search(
    query: str,
    client: ComposerClient | None = None,
    *,
    price_min: float | None = None,
    price_max: float | None = None,
    pages: int | None = None,
) -> list[SearchItem]:
    """Парсит поиск Ozon через Composer API (1–N страниц). В DRY_RUN — моки."""
    if Config.DRY_RUN:
        logger.info("dry_run_search", query=query)
        return mock_search_items()[: Config.SEARCH_ITEMS_LIMIT]

    owns = client is None
    client = client or ComposerClient()
    page_count = pages if pages is not None else Config.SEARCH_PAGES
    try:
        collected: list[SearchItem] = []
        seen: set[str] = set()
        for page in range(1, max(1, page_count) + 1):
            path = _build_search_path(
                query,
                price_min=price_min,
                price_max=price_max,
                page=page,
            )
            logger.info("search_path", path=path, page=page)
            payload = await client.fetch_page(path)
            if not payload:
                if page == 1:
                    logger.warning("search_empty_payload", query=query)
                break
            items = extract_search_items(payload)
            for it in items:
                if it.product_id not in seen:
                    seen.add(it.product_id)
                    collected.append(it)
            if len(items) < 4:
                break
            if len(collected) >= Config.SEARCH_ITEMS_LIMIT:
                break
        logger.info("search_parsed", query=query, count=len(collected), pages=page_count)
        return collected[: Config.SEARCH_ITEMS_LIMIT]
    finally:
        if owns:
            await client.aclose()


def _build_search_path(
    query: str,
    *,
    price_min: float | None = None,
    price_max: float | None = None,
    page: int = 1,
) -> str:
    """URL поиска с серверным фильтром цены Ozon."""
    text = query.strip().replace(" ", "+")
    path = f"/search/?text={text}&from_global=true"
    lo = int(price_min) if price_min is not None else 0
    hi = int(price_max) if price_max is not None else 0
    if hi > 0:
        path += f"&currency_price={lo};{hi}"
        path += "&sorting=score"
    if page > 1:
        path += f"&page={page}"
    return path
