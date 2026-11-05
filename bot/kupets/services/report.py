"""Сводка для продавцов (ТЗ v2 §3.6, фича д): ниша → отчёт по выдаче.

Качество v2 (06.11, по рапорту Шефа «полезного почти ничего»): одна выдача
OR-запроса давала 4 карточки без цен. Теперь сводка собирает до 40 карточек
четырьмя запросами (общий + отдельно WB + отдельно ЯМ + «… купить»): у WB/ЯМ
цены в сниппетах есть в ~40% случаев, у ЯМ — рейтинги.

Только тариф «Маркетинг», лимит 100/мес (FEATURE_LIMITS["report"]).
Поиски пакета сводка НЕ расходует (только собственный счётчик) —
решение по экономике: 100 сводок × 4 запроса ≈ 360₽ worst-case из 1500₽.

build_report() и format-хелперы — чистые, тестируются в CI.
"""

from __future__ import annotations

import math
import re
import statistics
from collections import Counter

from engine.junk import is_junk_name
from models.product import SearchItem
from utils import get_logger

logger = get_logger("services.report")

MP_LABELS = {"ozon": "Ozon", "wb": "Wildberries", "yam": "Я.Маркет"}

# Запросы сводки: (вариация ниши, площадки). Раздельные WB/ЯМ повышают долю
# сниппетов с ценами/рейтингами.
REPORT_QUERIES: list[tuple[str, tuple[str, ...]]] = [
    ("", ("ozon", "wb", "yam")),   # общий: все площадки
    ("", ("wb",)),                 # отдельно WB (цены)
    ("", ("yam",)),                # отдельно ЯМ (цены + рейтинги)
    (" купить", ("ozon", "wb", "yam")),  # вариация — дорисовывает выдачу
]

PRICE_BUCKETS = (1000, 2000, 4000)  # границы распределения цен, ₽


async def collect_niche(
    niche: str, *, per_query_limit: int = 10
) -> tuple[list[SearchItem], list[float]]:
    """Собрать нишу четырьмя запросами: (карточки, ценовые сигнатуры).

    Карточки — товары с URL detail/product (дедуп по ID). Сигнатуры — цены
    из сниппетов каталогов/тегов WB и Ozon («-75%. 1 087 ₽ 4 447 ₽»):
    не привязаны к конкретной карточке, но честно показывают рынок.
    """
    from parser.firecrawl_search import (
        _marketplace_of,
        _extract_price,
        _parse_results,
        FirecrawlClient,
    )

    fc = FirecrawlClient()
    items: list[SearchItem] = []
    samples: list[float] = []
    seen: set[str] = set()
    try:
        for suffix, marketplaces in REPORT_QUERIES:
            query = f"{niche.strip()}{suffix}"
            try:
                raw = await fc.search_web(
                    query, limit=per_query_limit, marketplaces=marketplaces
                )
            except Exception as exc:
                logger.warning("report_query_fail", extra={"error": str(exc), "query": query[:60]})
                continue
            for it in _parse_results({"web": raw}):
                key = it.product_id or it.url
                if key in seen:
                    continue
                seen.add(key)
                items.append(it)
            for r in raw:
                url = r.get("url", "")
                if "global.wildberries" in url:  # казахстанский WB — цены в ₸
                    continue
                price = _extract_price(r.get("description", "")) or _extract_price(
                    r.get("title", "")
                )
                if price and 100 <= price <= 500000:
                    samples.append(price)
    finally:
        await fc.aclose()
    logger.info(
        "report_collected",
        extra={"niche": niche[:40], "cards": len(items), "samples": len(samples)},
    )
    return items, samples




def _quant_label(price: float) -> str:
    """Человекочитаемый ценовой диапазон для распределения."""
    if price < PRICE_BUCKETS[0]:
        return f"менее {PRICE_BUCKETS[0]}₽"
    for lo, hi in zip(PRICE_BUCKETS, PRICE_BUCKETS[1:]):
        if price <= hi:
            return f"{lo}–{hi}₽"
    return f"больше {PRICE_BUCKETS[-1]}₽"


REPORT_CAPTION_RE = re.compile(
    r"(сводк|отчет|отчёт|анализ\s+ниши|^\s*report\b)", re.IGNORECASE
)


def should_report_caption(caption: str | None) -> bool:
    """Подпись к снимку просит сводку? («сводка», «отчёт», «report»…)."""
    return bool(caption and REPORT_CAPTION_RE.search(caption))


def build_report(
    niche: str,
    items: list[SearchItem],
    price_samples: list[float] | None = None,
) -> dict:
    """Статистика ниши: карточки, коридор, медианы по площадкам,
    распределение цен, рейтинги, конкуренция, демпинг, клоны, топ."""
    # Два прохода: черновая медиана по всем ценам → отсев демпинга (<30%) →
    # финальная статистика коридора по «нормальным» ценам (демпинг не искажает).
    all_priced = [it.price for it in items if it.price > 0] + list(price_samples or [])
    rough_med = statistics.median(all_priced) if all_priced else 0.0
    normal_items = [
        it for it in items
        if it.price > 0 and not (rough_med and it.price < rough_med * 0.3)
    ]
    normal_prices = [it.price for it in normal_items]
    clean_samples = [
        p for p in (price_samples or [])
        if not (rough_med and p < rough_med * 0.3)
    ]
    priced = sorted(normal_prices + clean_samples)
    med = statistics.median(priced) if priced else 0.0

    per_mp: Counter[str] = Counter(getattr(it, "marketplace", "ozon") for it in items)
    mp_medians: dict[str, dict] = {}
    for mp in per_mp:
        mp_prices = [
            it.price for it in normal_items
            if getattr(it, "marketplace", "") == mp
        ]
        if mp_prices:
            mp_medians[mp] = {
                "count": len(mp_prices),
                "median": statistics.median(mp_prices),
                "min": min(mp_prices),
            }

    buckets: Counter[str] = Counter(_quant_label(p) for p in priced)

    rated = [it for it in items if it.rating]
    rooted = [it for it in items if (it.reviews_count or 0) > 100]
    dump = [it for it in items if 0 < it.price < rough_med * 0.3] if rough_med else []
    clones = [it.name for it in items if is_junk_name(it.name, smartphone_query=False)]

    top = sorted(
        items,
        key=lambda it: (
            (it.rating or 0) * math.log10((it.reviews_count or 0) + 1),
            it.price > 0,
        ),
        reverse=True,
    )[:10]

    n = len(items)
    competition = (
        "низкая (карточек мало — ниша пустая)" if n < 8
        else "средняя" if n <= 25
        else "высокая (карточек много — нужен сильный оффер)"
    )
    recommended = round(med * 0.95 / 10) * 10 if med else 0

    return {
        "niche": niche,
        "total": n,
        "per_marketplace": dict(per_mp),
        "mp_medians": mp_medians,
        "min": priced[0] if priced else 0,
        "median": med,
        "max": priced[-1] if priced else 0,
        "priced_count": len(priced),
        "sample_count": len(clean_samples),
        "price_buckets": dict(buckets),
        "recommended_price": recommended,
        "rated_count": len(rated),
        "avg_rating": round(statistics.mean([it.rating for it in rated]), 2) if rated else 0,
        "rooted_count": len(rooted),  # карточки с >100 отзывов — «укоренившиеся»
        "dump_count": len(dump),
        "dump_names": [it.name[:40] for it in dump[:3]],
        "clone_count": len(clones),
        "clone_names": clones[:3],
        "top": top,
        "competition": competition,
    }


def stats_for_ai(stats: dict) -> str:
    """Статистика ниши текстом для ИИ-резюме (summarize_report)."""
    mp_labels = {"ozon": "Ozon", "wb": "Wildberries", "yam": "Я.Маркет"}
    parts = [
        f"Ниша: {stats['niche']}",
        f"Карточек собрано: {stats['total']}",
    ]
    if stats["per_marketplace"]:
        parts.append("По площадкам: " + ", ".join(
            f"{mp_labels.get(k, k)} — {v}" for k, v in stats["per_marketplace"].items()
        ))
    if stats["median"]:
        parts.append(
            f"Цены: от {stats['min']:.0f}₽, медиана {stats['median']:.0f}₽, до {stats['max']:.0f}₽"
        )
    for mp, d in (stats.get("mp_medians") or {}).items():
        parts.append(f"Медиана {mp_labels.get(mp, mp)}: {d['median']:.0f}₽ ({d['count']} карточек)")
    if stats.get("recommended_price"):
        parts.append(f"Рекомендуемая цена входа: ~{stats['recommended_price']}₽")
    if stats.get("avg_rating"):
        parts.append(f"Средний рейтинг: {stats['avg_rating']} (у {stats['rated_count']} карточек)")
    parts.append(f"Конкуренция: {stats['competition']}")
    if stats.get("rooted_count"):
        parts.append(f"Карточек с >100 отзывов: {stats['rooted_count']}")
    if stats.get("dump_count"):
        parts.append(f"Демпинг (<30% медианы): {stats['dump_count']}")
    if stats.get("clone_count"):
        parts.append(f"Клоны/мусорные названия: {stats['clone_count']}")
    return "\n".join(parts)


def niche_queries_count() -> int:
    """Сколько поисковых запросов делает одна сводка (для сообщений)."""
    return len(REPORT_QUERIES)
