"""Отсев мусорных/нерелевантных карточек по названию."""

from __future__ import annotations

import re

from models.product import ProductData, SearchItem

# Явный мусор / нецелевые категории для запросов про смартфоны/электронику
JUNK_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"тв[-\s]?приставк",
        r"медиаплеер",
        r"android\s*tv|андройд\s*тв|smart\s*tv\s*box",
        r"чехол|стекл[оа]|плёнк|пленк|защитн",
        r"кабель|зарядк|адаптер|блок\s*питан",
        r"наушник|гарнитур|холдер|штатив",
        r"в\s*качестве\s*подарка",
        r"\bi\d{1,2}\s*pro\s*max\b",  # клоны iPhone I15 Pro Max
        r"\biphone\s*\d",  # часто контрафакт в дешёвом сегменте без Apple
        r"реплик|копи[яи]|noname|no\s*name",
        r"смарт[-\s]?часы|фитнес[-\s]?браслет",
        r"планшет(?!\s*н)",  # планшеты при поиске смартфона
    )
]

CLONE_BRANDS = re.compile(
    r"\b(i15|i14|i13|i16|reno\s*phone|super\s*phone)\b",
    re.IGNORECASE,
)


def is_junk_name(name, smartphone_query=False):
    """Возвращает причину отсева или None, если ок."""
    if not name:
        return "пустое название"
    low = name.lower()
    for pat in JUNK_PATTERNS:
        if pat.search(name):
            # iPhone с брендом Apple — оригинал, не клон
            if pat.pattern.startswith(r"\biphone\s*") and "apple" in low:
                continue
            return "нерелевантный товар / аксессуар / клон"
    if smartphone_query and CLONE_BRANDS.search(name):
        return "подозрительный клон смартфона"
    return None


def filter_junk_items(
    items: list[SearchItem],
    *,
    smartphone_query: bool = False,
) -> tuple[list[SearchItem], list[tuple[SearchItem, str]]]:
    kept: list[SearchItem] = []
    junk: list[tuple[SearchItem, str]] = []
    for it in items:
        reason = is_junk_name(it.name, smartphone_query=smartphone_query)
        if reason:
            junk.append((it, reason))
        else:
            kept.append(it)
    return kept, junk


def junk_reason_for_product(
    product: ProductData,
    *,
    smartphone_query: bool = False,
) -> str | None:
    return is_junk_name(product.name, smartphone_query=smartphone_query)
