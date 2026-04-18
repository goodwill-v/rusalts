"""
Снятие Markdown/технических обёрток с публичного текста новостей.
Используется воркером контента и при финальной публикации (сайт + ВК), чтобы не уходил сырой md в эфир.
"""

from __future__ import annotations

import re

_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)


def clean_public_text_fragment(s: str) -> str:
    t = (s or "").strip()
    if not t:
        return ""
    t = _FENCED_JSON_RE.sub("", t)
    t = t.replace("```", "").strip()
    t = re.sub(r"(?im)^\s*internal_note\s*[:=].*$", "", t).strip()
    t = "\n".join(line.rstrip() for line in t.splitlines()).strip()
    return t


def strip_markdown_public(s: str) -> str:
    """Плоский текст для сайта и ВК: убираем типичный Markdown."""
    t = clean_public_text_fragment(s)
    if not t:
        return ""
    for _ in range(16):
        prev = t
        t = re.sub(r"(?m)^#{1,6}\s+", "", t)
        t = re.sub(r"\*\*([^*]+)\*\*", r"\1", t)
        t = re.sub(r"`([^`]+)`", r"\1", t)
        t = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"\1 — \2", t)
        t = re.sub(r"(?m)^\s*[-*]\s+", "", t)
        if t == prev:
            break
    t = "\n".join(line.rstrip() for line in t.splitlines()).strip()
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t
