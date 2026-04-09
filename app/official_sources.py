from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from app import config


@dataclass(frozen=True)
class OfficialExcerpt:
    title: str
    url: str
    excerpt: str


_SPACE_RE = re.compile(r"\s+")


def _strip_html_to_text(html: str) -> str:
    # Minimal safe extraction (no JS execution).
    html = re.sub(r"(?is)<(script|style|noscript)\b.*?>.*?</\1>", " ", html)
    m = re.search(r"(?is)<(main|article)\b.*?>.*?</\1>", html)
    if m:
        html = m.group(0)
    text = re.sub(r"(?is)<[^>]+>", " ", html)
    text = _SPACE_RE.sub(" ", text).strip()
    return text


def _tokenize(q: str) -> list[str]:
    q = (q or "").strip().lower()
    parts = re.findall(r"[a-zа-яё0-9]{3,}", q, flags=re.IGNORECASE)
    # de-dup while preserving order
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out[:12]


def _load_sources(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    items = raw.get("sources") or []
    return [x for x in items if isinstance(x, dict) and x.get("url")]


async def search_official_sources(
    *,
    query: str,
    sources_path: Path | None = None,
    max_fetch: int = 4,
    timeout_s: float = 12.0,
    max_chars_per_source: int = 14_000,
) -> list[OfficialExcerpt]:
    """
    Very lightweight 'official sources' lookup:
    - fetch top-N sources (priority/high & level=1 first)
    - find query tokens in extracted text
    - return short excerpts with URLs

    This is intentionally conservative: if we can't find a snippet, we won't claim an answer.
    """
    tokens = _tokenize(query)
    if not tokens:
        return []

    sp = sources_path or (config.BASE_DIR / "parser" / "sources.json")
    sources = _load_sources(sp)
    if not sources:
        return []

    def rank(s: dict[str, Any]) -> tuple[int, int]:
        pr = str(s.get("priority") or "").strip().lower()
        lvl = int(s.get("level") or 3)
        pr_score = 0 if pr == "high" else (1 if pr == "medium" else 2)
        return (pr_score, lvl)

    sources_sorted = sorted(sources, key=rank)
    picked = sources_sorted[: max(1, int(max_fetch))]

    out: list[OfficialExcerpt] = []
    async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=True) as client:
        for s in picked:
            url = str(s.get("url") or "").strip()
            title = str(s.get("title") or s.get("id") or url).strip()
            if not url:
                continue
            try:
                r = await client.get(url, headers={"User-Agent": "ALT-Expert/0.1 (official-sources; contact: internal)"})
            except Exception:
                continue
            if r.status_code >= 400:
                continue
            text = _strip_html_to_text(r.text or "")
            if not text:
                continue
            text = text[:max_chars_per_source]
            low = text.lower()

            # Find the first token match window.
            pos = -1
            hit = ""
            for t in tokens:
                p = low.find(t)
                if p != -1:
                    pos = p
                    hit = t
                    break
            if pos == -1:
                continue

            start = max(0, pos - 240)
            end = min(len(text), pos + 520)
            excerpt = text[start:end].strip()
            if hit and hit not in excerpt.lower():
                # Extremely defensive; should not happen but keep honest.
                continue
            out.append(OfficialExcerpt(title=title, url=url, excerpt=excerpt))

    return out[:6]

