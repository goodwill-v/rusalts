"""Скоринг тем и значимости для новостного дайджеста АЛТ (ТЗ «Автоконтент v2», пп. 2.4–2.5).

Ключевые темы в порядке приоритета Шефа:
1. ИИ-развитие → 2. Кейсы ИИ → 3. Автоматизация/автономизация →
4. Мессенджеры (правило МАКС) → 5. Законодательство бизнеса → 6. ИТ-курьёзы.
Особое внимание — отечественным продуктам (origin=ru).

Модуль чистый (без I/O и сети) — легко покрывается unit-тестами.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.news_feeds import NewsItem, keyword_hit, keywords_any_hit

# Минимальный балл, чтобы кандидат попал в дайджест (фильтр «лишнее не попадает»).
MIN_TOPIC_SCORE = 8

# Бонус отечественным источникам/новостям (ТЗ 2.5: «особое внимание — РФ»).
ORIGIN_RU_BONUS = 5

_PRIORITY_BONUS = {"high": 4, "medium": 2, "low": 0}

# Слова «значимости»: крупные релизы ИИ (ТЗ 2.4: «крупный релиз ИИ»).
_MAJOR_PLAYERS = ["openai", "anthropic", "google", "gemini", "gpt", "claude", "deepseek", "qwen", "llama", "meta", "microsoft", "nvidia"]
_MAJOR_PLAYERS_RU = ["сбер", "яндекс", "вк", "мтс", "газпром", "минцифры", "российск"]
_RELEASE_VERBS = ["представил", "представила", "выпустил", "выпустила", "запустил", "запустила", "релиз", "открыл доступ", "анонсировал", "анонсировала", "unveiled", "released", "launched"]

# Законодательство, влияющее на бизнес (ТЗ 2.5.5).
_LEGAL_WORDS = ["152-фз", "168-фз", "265-фз", "фз", "закон", "законопроект", "постановление", "роскомнадзор", "ркн", "минцифры", "штраф", "обязан", "требован", "регламент", "запрет", "запретит", "разрешит"]
_BUSINESS_WORDS = ["бизнес", "компани", "предпринимател", "рынок", "банк", "стартап", "клиент", "продаж", "процесс"]

# Кейсы с цифрами (ТЗ 2.5.2: «кейсы с цифрами»).
_NUMBER_RE = re.compile(r"\d+\s*%|\d+\s*(млн|млрд|тыс)|в\s+\d+\s*раз|х\d|\d{2,}", re.IGNORECASE)


@dataclass
class ScoredCandidate:
    """Кандидат в дайджест с посчитанным скорингом."""

    item: NewsItem
    score: int = 0
    topics: list[str] = field(default_factory=list)  # topic_id в порядке приоритета
    topic_labels: list[str] = field(default_factory=list)
    high_significance: bool = False
    reasons: list[str] = field(default_factory=list)  # коды причин значимости
    is_max: bool = False  # новость про мессенджер МАКС → недельная сводка, не дайджест

    @property
    def uid(self) -> str:
        return self.item.uid


def _topic_weight(order: int) -> int:
    """Вес темы по порядку приоритета ТЗ 2.5: 1→6, 2→5, … 6→1."""
    return max(1, 7 - int(order))


def _is_max_text(text: str) -> bool:
    """Про мессенджер МАКС: слово «макс»/«max» как отдельное слово (не «максимум»)."""
    return keyword_hit(text, "макс") or keyword_hit(text, "max") or "max.ru" in text.lower() or "dev.max" in text.lower()


def is_max_news(item: NewsItem) -> bool:
    """Правило ТЗ 2.3: новости про МАКС копятся до еженедельного релиза-сводки."""
    blob = f"{item.title} {item.summary}"
    return "messengers" in (item.topics or []) and _is_max_text(blob)


def _significance_reasons(title: str, summary: str, topics: list[str]) -> list[str]:
    """Критерии «высокой значимости» (ТЗ 2.4): влияет на бизнес-процессы/законодательство/крупный релиз ИИ."""
    blob = f"{title} {summary}"
    tl = title.lower()
    reasons: list[str] = []

    legal_hit = keywords_any_hit(blob, _LEGAL_WORDS)
    biz_hit = keywords_any_hit(blob, _BUSINESS_WORDS)
    if legal_hit and (biz_hit or "legal" in topics):
        reasons.append("legal_business")

    player_hit = keywords_any_hit(blob, _MAJOR_PLAYERS) or keywords_any_hit(blob, _MAJOR_PLAYERS_RU)
    verb_hit = keywords_any_hit(blob, _RELEASE_VERBS)
    if player_hit and verb_hit and "ai_dev" in topics:
        reasons.append("ai_major_release")

    if "ai_case" in topics and _NUMBER_RE.search(blob) and keywords_any_hit(blob, ["вырос", "снизил", "сэкономил", "сократил", "повысил", "ускорил", "roi", "эффективност"]):
        reasons.append("case_with_numbers")

    if "automation" in topics and biz_hit and _NUMBER_RE.search(blob):
        reasons.append("automation_impact")

    return reasons


def score_candidate(
    item: NewsItem,
    topic_cfg: dict[str, dict[str, Any]],
    *,
    source_priority: str = "medium",
) -> ScoredCandidate:
    """Скоринг одного кандидата: темы (заголовок весит вдвое) + приоритет источника + РФ-бонус."""
    scored = ScoredCandidate(item=item)
    scored.is_max = is_max_news(item)

    title = item.title or ""
    summary = item.summary or ""
    best_topics: list[tuple[int, str, str]] = []

    total = 0
    for topic_id, cfg in topic_cfg.items():
        kws = [str(k) for k in (cfg.get("keywords") or []) if str(k).strip()]
        if not kws:
            continue
        t_hits = sum(1 for kw in kws if keyword_hit(title, kw))
        s_hits = sum(1 for kw in kws if keyword_hit(summary, kw))
        hits = t_hits * 2 + s_hits
        if hits <= 0:
            continue
        order = int(cfg.get("order") or 9)
        weight = _topic_weight(order)
        total += weight * min(hits, 4)
        best_topics.append((order, topic_id, str(cfg.get("label") or topic_id)))

    total += _PRIORITY_BONUS.get(source_priority, 0)
    if item.origin == "ru":
        total += ORIGIN_RU_BONUS

    best_topics.sort()
    scored.score = total
    scored.topics = [t[1] for t in best_topics]
    scored.topic_labels = [t[2] for t in best_topics]
    scored.reasons = _significance_reasons(title, summary, scored.topics) if not scored.is_max else []
    scored.high_significance = bool(scored.reasons) and total >= MIN_TOPIC_SCORE
    return scored


def rank_candidates(
    scored: list[ScoredCandidate],
    *,
    min_score: int = MIN_TOPIC_SCORE,
) -> list[ScoredCandidate]:
    """Отсортировать валидных кандидатов: значимые выше, затем балл, затем РФ, затем свежесть."""
    valid = [s for s in scored if s.score >= min_score and s.topics]
    valid.sort(
        key=lambda s: (
            1 if s.high_significance else 0,  # значимые выше
            s.score,
            1 if s.item.origin == "ru" else 0,
            s.item.published or s.item.fetched_at_utc,
        ),
        reverse=True,
    )
    return valid


def select_for_digest(
    ranked: list[ScoredCandidate],
    *,
    min_news: int = 3,
    max_news: int = 6,
    max_per_topic: int = 2,
) -> list[ScoredCandidate]:
    """Отбор 3–6 новостей в дайджест: МАКС-новости исключены всегда, тематика разнообразна."""
    per_topic: dict[str, int] = {}
    out: list[ScoredCandidate] = []
    for s in ranked:
        if s.is_max:
            continue  # правило МАКС (ТЗ 2.3) — приоритетнее всего
        primary = s.topics[0] if s.topics else ""
        if primary and per_topic.get(primary, 0) >= max_per_topic:
            continue
        out.append(s)
        if primary:
            per_topic[primary] = per_topic.get(primary, 0) + 1
        if len(out) >= max_news:
            break
    return out
