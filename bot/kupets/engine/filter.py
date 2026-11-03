"""Фильтры отсечения: passed / rejected / near_miss."""

from __future__ import annotations

from dataclasses import dataclass

from engine.junk import junk_reason_for_product
from engine.normalizer import median
from models.product import ProductData
from models.result import SearchFilters
from utils import get_logger

logger = get_logger("engine.filter")


@dataclass
class FilterBuckets:
    passed: list[ProductData]
    rejected: list[tuple[ProductData, list[str]]]
    near_miss: list[tuple[ProductData, list[str]]]


def apply_filters(
    products: list[ProductData],
    filters: SearchFilters,
    *,
    smartphone_query: bool = False,
) -> FilterBuckets:
    """Сначала общие фильтры, затем характеристики."""
    if not products:
        return FilterBuckets([], [], [])

    # Медиану считаем по товарам в ценовом диапазоне запроса —
    # иначе флагманы/фейки ломают пороги. Товары без цены (price=0, «цена по
    # ссылке») в медиану и ценовые фильтры не попадают.
    if filters.price_max is not None or filters.price_min is not None:
        tmp = []
        for p in products:
            if p.price <= 0:
                continue
            if filters.price_max is not None and p.price > filters.price_max:
                continue
            if filters.price_min is not None and p.price < filters.price_min:
                continue
            tmp.append(p)
        priced = tmp if tmp else [p for p in products if p.price > 0]
    else:
        priced = [p for p in products if p.price > 0]

    prices = [p.price for p in priced if p.price > 0]
    med = median(prices) if prices else 0.0

    passed: list[ProductData] = []
    rejected: list[tuple[ProductData, list[str]]] = []
    near_miss: list[tuple[ProductData, list[str]]] = []

    for p in products:
        hard: list[str] = []
        soft: list[str] = []

        junk = junk_reason_for_product(p, smartphone_query=smartphone_query)
        if junk:
            hard.append(junk)

        if filters.only_fbo and p.delivery_type != "FBO":
            hard.append("не FBO (склад Ozon)")

        # Ценовые фильтры — только когда цена известна (price=0 = «по ссылке»)
        if p.price > 0:
            if filters.price_max is not None and p.price > filters.price_max:
                hard.append(f"цена выше {int(filters.price_max)} руб")
            if filters.price_min is not None and p.price < filters.price_min:
                hard.append(f"цена ниже {int(filters.price_min)} руб")

        price_ok = not any("цена выше" in r or "цена ниже" in r for r in hard)
        in_budget = price_ok and junk is None

        popular = (p.reviews_count or 0) >= 200 and (p.rating or 0) >= 4.0
        if in_budget and med > 0 and 0 < p.price < med * 0.3 and not popular:
            hard.append("подозрительно низкая цена")
        if in_budget and med > 0 and p.price > med * 2.5:
            hard.append("цена завышена")
        if (
            in_budget
            and p.old_price
            and p.old_price > 0
            and p.price / p.old_price < 0.3
            and not popular
        ):
            hard.append("подозрительная скидка")

        if p.seller and p.seller.rating is not None and p.seller.rating < 1.0:
            hard.append("низкий рейтинг продавца")

        if (
            p.reviews_count == 0
            and p.seller
            and p.seller.orders_count is not None
            and p.seller.orders_count < 10
        ):
            hard.append("нет отзывов, продавец новый")

        if (
            p.rating is not None
            and p.rating < 3.5
            and p.reviews_count > 10
        ):
            hard.append("низкий рейтинг товара")

        # Характеристики
        if filters.ram_gb is not None:
            if p.ram_gb is None:
                soft.append("характеристики не подтверждены (ОЗУ)")
            elif p.ram_gb < filters.ram_gb:
                hard.append(f"оперативка {p.ram_gb:g}ГБ < {filters.ram_gb:g}ГБ")

        if filters.storage_gb is not None:
            if p.storage_gb is None:
                soft.append("характеристики не подтверждены (память)")
            elif p.storage_gb < filters.storage_gb:
                hard.append(
                    f"память {p.storage_gb:g}ГБ < {filters.storage_gb:g}ГБ"
                )

        if filters.screen_inches is not None:
            if p.screen_inches is None:
                soft.append("характеристики не подтверждены (экран)")
            elif p.screen_inches < filters.screen_inches:
                hard.append(
                    f'экран {p.screen_inches:g}" < {filters.screen_inches:g}"'
                )

        if filters.brand and filters.brand.lower() not in p.name.lower():
            soft.append(f"бренд может не совпадать ({filters.brand})")

        p.rejection_reasons = hard
        p.near_miss_reasons = soft

        if hard:
            rejected.append((p, hard))
        elif soft:
            near_miss.append((p, soft))
        else:
            passed.append(p)

    logger.info(
        "filter_done",
        passed=len(passed),
        rejected=len(rejected),
        near_miss=len(near_miss),
        median=med,
    )
    return FilterBuckets(passed=passed, rejected=rejected, near_miss=near_miss)
