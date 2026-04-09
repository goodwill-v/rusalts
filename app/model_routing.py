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


@dataclass(frozen=True)
class ModelChoice:
    model: str
    reason: str


def looks_legal(text: str) -> bool:
    return bool(_LEGAL_RE.search(text or ""))


def choose_main_or_heavy(*, main: str, heavy: str, escalate: bool, reason: str) -> ModelChoice:
    main = (main or "").strip()
    heavy = (heavy or "").strip()
    if escalate and heavy:
        return ModelChoice(model=heavy, reason=reason)
    return ModelChoice(model=main, reason="main_default")


def backend_choice(*, text: str, kb_hits_count: int, main: str, heavy: str) -> ModelChoice:
    if looks_legal(text):
        return choose_main_or_heavy(main=main, heavy=heavy, escalate=True, reason="backend_legal_query")
    # If we have very few KB hits but still attempt synthesis, allow escalation.
    if kb_hits_count <= 1 and heavy:
        return choose_main_or_heavy(main=main, heavy=heavy, escalate=True, reason="backend_low_kb_hits")
    return choose_main_or_heavy(main=main, heavy=heavy, escalate=False, reason="backend_default")


def content_choice(*, has_legal: bool, main: str, heavy: str) -> ModelChoice:
    if has_legal:
        return choose_main_or_heavy(main=main, heavy=heavy, escalate=True, reason="content_legal_change_package")
    return choose_main_or_heavy(main=main, heavy=heavy, escalate=False, reason="content_default")

