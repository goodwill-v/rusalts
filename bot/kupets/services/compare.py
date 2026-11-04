"""Сравнение «X vs Y» (ТЗ v2 §3.2/фича б): два поиска → таблица + итог.

Тратит 2 поиска из месячного пула пользователя (честно показываем в сообщении).
Чистые функции (parse/rank/verdict) — без зависимостей, тестируются в CI.
"""

from __future__ import annotations

import math
import re

from models.product import SearchItem
from utils import get_logger

logger = get_logger("services.compare")

VS_RE = re.compile(r"\bvs\b|\bпротив\b", re.IGNORECASE)
CONJ_RE = re.compile(r"\s+и\s+", re.IGNORECASE)


def parse_compare_query(text: str) -> tuple[str, str] | None:
    """«сравни A и B» / «сравни A vs B» → (A, B). None — не разбор.

    Сначала «vs», затем одиночное « и » (первое вхождение); стороны без
    глаголов поиска — иначе это не сравнение, а обычный запрос.
    """
    t = (text or "").strip()
    low = t.lower()
    body = None
    for prefix in ("сравни ", "сравнить "):
        if low.startswith(prefix):
            body = t[len(prefix):].strip()
            break
    if body is None:
        return None
    parts = VS_RE.split(body, maxsplit=1)
    if len(parts) == 2:
        a, b = parts[0].strip(" ?.,-"), parts[1].strip(" ?.,-")
    else:
        m = CONJ_RE.search(body)
        if not m:
            return None
        a, b = body[: m.start()].strip(), body[m.end():].strip()
    if not a or not b:
        return None
    return a, b


def _rank_key(it: SearchItem) -> float:
    """Предпочтение: с ценой и рейтингом, популярнее, дешевле."""
    rating = it.rating or 3.5
    reviews = max(it.reviews_count or 0, 1)
    price = max(it.price, 1.0)
    priced_bonus = 1.3 if it.price > 0 else 1.0
    return (rating * math.log10(reviews + 1) / math.sqrt(price)) * priced_bonus


def pick_best(items: list[SearchItem]) -> SearchItem | None:
    """Лучший представитель стороны: с ценой, рейтингом, отзывами.

    Товары с ценой приоритетны (rank делит на цену — позиция 0 цены ломает
    сортировку); среди них уже работаем ранком.
    """
    if not items:
        return None
    priced = [it for it in items if it.price > 0]
    return max(priced or items, key=_rank_key)


def compare_verdict(a: SearchItem | None, b: SearchItem | None) -> str:
    """Вычисляемая строка итога (без ИИ): цена/рейтинг/отзывы."""
    if a is None and b is None:
        return "По обе стороны ничего не нашлось — переформулируйте запросы."
    if a is None or b is None:
        side = "A" if a else "B"
        return f"Нашёл только сторону {side} — вторую переформулируйте."
    lines: list[str] = []
    if a.price > 0 and b.price > 0 and a.price != b.price:
        cheap, dear = (a, b) if a.price < b.price else (b, a)
        diff_pct = round(abs(a.price - b.price) / max(a.price, b.price) * 100)
        lines.append(
            f"Дешевле {cheap.name[:32]!r} — на {diff_pct}% "
            f"({int(cheap.price)}₽ против {int(dear.price)}₽)."
        )
    if (a.rating or 0) != (b.rating or 0):
        hi, lo = (a, b) if (a.rating or 0) > (b.rating or 0) else (b, a)
        if hi.rating:
            lines.append(f"Рейтинг выше у {hi.name[:32]!r} ({hi.rating} vs {lo.rating or '—'}).")
    if (a.reviews_count or 0) != (b.reviews_count or 0):
        hi, lo = (a, b) if (a.reviews_count or 0) > (b.reviews_count or 0) else (b, a)
        if hi.reviews_count:
            lines.append(f"Отзывов больше у {hi.name[:32]!r} ({hi.reviews_count}).")
    if not lines:
        lines.append("Цены/рейтинги в сниппетах не видны — смотрите по ссылкам.")
    return " ".join(lines)


def searches_needed() -> int:
    return 2
