"""Анти-клон проверка «/check» (ТЗ v2 фича в): вердикт по названию.

Эвристики из engine/junk.py (клоны I15 Pro Max и пр.) + цена против медианы
выдачи + отсутствие отзывов/рейтингов. Тратит 1 поиск из пула.
Чистая функция verdict_for() — тестируется в CI.
"""

from __future__ import annotations

import statistics

from engine.junk import is_junk_name
from models.product import SearchItem
from utils import get_logger

logger = get_logger("services.anticlone")

LEVELS = ("ok", "warn", "danger")


def verdict_for(name: str, items: list[SearchItem]) -> dict:
    """Вердикт по названию и выдаче: {level, reasons, confidence}.

    level: ok / warn / danger; confidence: высокая/средняя/низкая.
    """
    reasons: list[str] = []
    level = "ok"
    confidence = "средняя"

    # 1. Явные паттерны клона/аксессуара в самом запросе
    clone_reason = is_junk_name(name, smartphone_query=True)
    if clone_reason and ("клон" in clone_reason or "нерелевант" in clone_reason):
        low = name.lower()
        if any(w in low for w in ("i15", "i14", "i13", "i16", "pro max")) and \
                "apple" not in low and "iphone" not in low:
            return {
                "level": "danger",
                "reasons": [
                    "Название похоже на клон (I15/I16 Pro Max и т.п. — так маркируют "
                    "подделки, а не оригинальные товары Apple)",
                ],
                "confidence": "высокая",
            }
        level = "warn"
        reasons.append(clone_reason)
        confidence = "средняя"

    # 2. Цена: лучшая цена выдачи против медианы всех цен
    priced = [it.price for it in items if it.price > 0]
    if len(priced) >= 3:
        med = statistics.median(priced)
        best = min(priced)
        if med > 0 and best < med * 0.3:
            if LEVELS.index(level) < LEVELS.index("warn"):
                level = "warn"
            reasons.append(
                f"Минимальная цена в выдаче ({int(best)}₽) — менее 30% медианы "
                f"({int(med)}₽): типичный признак фейка или обмана"
            )
            confidence = "высокая"

    # 3. Рейтинги/отзывы вообще не видны — осторожно
    rated = [it for it in items if it.rating]
    if items and not rated:
        if level == "ok":
            level = "warn"
            reasons.append("В выдаче нет товаров с рейтингом/отзывами — "
                           "проверьте продавца по ссылке перед оплатой")
        confidence = "низкая"

    if not reasons:
        reasons.append("Явных признаков подделки в названии и выдаче не нашёл. "
                       "Это не гарантия: смотрите рейтинг продавца и отзывы")
    return {"level": level, "reasons": reasons, "confidence": confidence}


def searches_needed() -> int:
    return 1
