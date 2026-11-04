"""Подарочный подбор (ТЗ v2 фича г): описание + бюджет → 3–4 товарных
запроса (RouterAI) → поиск → подборка карточек.

Тратит до 4 поисков из месячного пула (по числу запросов). Чистые функции
parse/format — тестируются в CI.
"""

from __future__ import annotations

import re

from models.product import SearchItem
from services.compare import pick_best
from utils import get_logger

logger = get_logger("services.gift")

BUDGET_RE = re.compile(r"до\s+(\d[\d\s]*)(?:\s*(?:₽|руб|рублей|р\b))?", re.IGNORECASE)


def parse_gift_request(text: str) -> tuple[str, int] | None:
    """«маме до 2000» → («маме», 2000). None — нет описания или бюджета."""
    t = (text or "").strip()
    if not t:
        return None
    m = BUDGET_RE.search(t)
    if not m:
        return None
    budget = int(m.group(1).replace(" ", "").replace("\u202f", ""))
    who = (t[: m.start()] + t[m.end():]).strip(" ,.;:")
    who = re.sub(r"\s{2,}", " ", who)
    if not who:
        return None
    return who, budget


def queries_from_llm(text: str, limit: int = 4) -> list[str]:
    """Ответ LLM → список запросов (нумерованный или построчный)."""
    found = re.findall(r"(?:^|\n)\s*(?:\d+[\)\.\-:]|\-)\s*(.+)", text or "")
    queries = [q.strip(" -") for q in (found or (text or "").split("\n")) if q.strip()]
    return queries[:limit]


def max_searches() -> int:
    return 4


def pick_line(q: str, best: SearchItem | None) -> str:
    """Строка подборки для одного запроса (формат в messages, тут — данные)."""
    return q


def best_items(queries: list[str], results: dict[str, list[SearchItem]]) -> list[tuple[str, SearchItem | None]]:
    return [(q, pick_best(results.get(q, []))) for q in queries]
