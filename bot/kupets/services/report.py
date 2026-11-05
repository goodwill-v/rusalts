"""Сводка для продавцов (ТЗ v2 §3.6, фича д): ниша → отчёт по выдаче.

Только тариф «Маркетинг», 10/мес (FEATURE_LIMITS["report"]). Тратит 1 поиск
из пула. build_report() — чистая функция, тестируется в CI.
"""

from __future__ import annotations

import statistics
from collections import Counter

from engine.junk import is_junk_name
from models.product import SearchItem

MP_LABELS = {"ozon": "Ozon", "wb": "Wildberries", "yam": "Я.Маркет"}


REPORT_CAPTION_RE = __import__("re").compile(
    r"(сводк|отчет|отчёт|анализ\s+ниши|^\s*report\b)", __import__("re").IGNORECASE
)


def should_report_caption(caption: str | None) -> bool:
    """Подпись к снимку просит сводку? («сводка», «отчёт», «report»…).

    Так работает инструкция для продавцов: фото товара с подписью
    «сводка» → анализ ниши по распознанному запросу.
    """
    return bool(caption and REPORT_CAPTION_RE.search(caption))


def build_report(niche: str, items: list[SearchItem]) -> dict:
    """Статистика ниши из поисковой выдачи (карточки, не категории)."""
    priced = sorted(it.price for it in items if it.price > 0)
    per_mp = Counter(
        getattr(it, "marketplace", "ozon") for it in items
    )
    clones = [
        it.name for it in items
        if is_junk_name(it.name, smartphone_query=False)
    ]
    med = statistics.median(priced) if priced else 0.0
    dump = [it for it in items if 0 < it.price < med * 0.3] if med else []
    # топ-10: рейтинг × log(отзывы), с ценой приоритет
    ranked = sorted(
        items,
        key=lambda it: (
            (it.rating or 0) * __import__("math").log10((it.reviews_count or 0) + 1),
            it.price > 0,
        ),
        reverse=True,
    )[:10]
    return {
        "niche": niche,
        "total": len(items),
        "per_marketplace": dict(per_mp),
        "min": priced[0] if priced else 0,
        "median": med,
        "max": priced[-1] if priced else 0,
        "priced_count": len(priced),
        "dump_count": len(dump),
        "dump_names": [it.name[:40] for it in dump[:3]],
        "clone_count": len(clones),
        "clone_names": clones[:3],
        "top": ranked,
    }
