from __future__ import annotations

import re
from dataclasses import dataclass


_LEGAL_RE = re.compile(
    r"\b("
    r"152-?фз|"
    r"персональн(ые|ых)\s+данн|пдн|"
    r"роскомнадзор|ркн|"
    r"согласие\s+на\s+обработк|"
    r"локализац(ия|ии)\s+данн|"
    r"трансграничн|"
    r"оператор\s+персональн|"
    r"реестр|уведомлени(е|я)|"
    r"комплаенс|"
    r"закон|постановлени(е|я)|приказ|регламент"
    r")\b",
    re.IGNORECASE,
)

# Вопросы в тематике проекта АЛТ (миграция на MAX, экосистема, боты, смежная регуляторика).
_PROJECT_TOPIC_RE = re.compile(
    r"\b("
    r"макс\b|max\b|max\.ru|dev\.max|"
    r"мессенджер|миграц|telegram|телеграм|телеграмм|whatsapp|вотсап|"
    r"вконтакте|вк\s+мессендж|vk\b|vkontakte|"
    r"сферум|sferum|"
    r"альтернатив|а\s*л\s*т\b|российск(ий|ого)\s+номер|сбп|госуслуг|"
    r"чат-?бот|sdk|mini\s*apps?|"
    r"платформ(а|ы)\s+(макс|max)|"
    r"rustore|рустор"
    r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ModelChoice:
    model: str
    reason: str


def looks_legal(text: str) -> bool:
    return bool(_LEGAL_RE.search(text or ""))


def is_alt_project_topic(text: str) -> bool:
    """Тематика консультанта АЛТ: MAX/миграция/экосистема или смежная регуляторика."""
    t = text or ""
    return looks_legal(t) or bool(_PROJECT_TOPIC_RE.search(t))


def choose_main_or_heavy(*, main: str, heavy: str, escalate: bool, reason: str) -> ModelChoice:
    main = (main or "").strip()
    heavy = (heavy or "").strip()
    if escalate and heavy:
        return ModelChoice(model=heavy, reason=reason)
    return ModelChoice(model=main, reason="main_default")


def backend_choice(*, text: str, kb_hits_count: int, main: str, heavy: str) -> ModelChoice:
    if looks_legal(text):
        return choose_main_or_heavy(main=main, heavy=heavy, escalate=True, reason="backend_legal_query")
    # Ровно один слабый хит RAG — повышаем риск галлюцинаций → heavy (если задан).
    if kb_hits_count == 1 and heavy:
        return choose_main_or_heavy(main=main, heavy=heavy, escalate=True, reason="backend_low_kb_hits")
    return choose_main_or_heavy(main=main, heavy=heavy, escalate=False, reason="backend_default")


def content_choice(*, has_legal: bool, main: str, heavy: str) -> ModelChoice:
    if has_legal:
        return choose_main_or_heavy(main=main, heavy=heavy, escalate=True, reason="content_legal_change_package")
    return choose_main_or_heavy(main=main, heavy=heavy, escalate=False, reason="content_default")

