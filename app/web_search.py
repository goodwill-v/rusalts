from __future__ import annotations

import html as html_mod
import re
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class WebSnippet:
    title: str
    url: str
    excerpt: str


_SPACE_RE = re.compile(r"\s+")
_WEB_RESULT_BLOCK = re.compile(
    r'<div class="result[^"]*web-result[^"]*"[^>]*>(.*?)(?=<div class="result[^"]*web-result|\Z)',
    re.DOTALL | re.IGNORECASE,
)


def _clip(s: str, n: int) -> str:
    s = (s or "").strip()
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


def _strip_tags(s: str) -> str:
    s = re.sub(r"(?is)<[^>]+>", " ", s)
    return _SPACE_RE.sub(" ", html_mod.unescape(s)).strip()


def _norm_url(u: str) -> str:
    u = html_mod.unescape((u or "").strip()).replace("&amp;", "&")
    return u.split("#")[0]


async def _ddg_instant_answer(client: httpx.AsyncClient, query: str, max_snippets: int) -> list[WebSnippet]:
    out: list[WebSnippet] = []
    try:
        r = await client.get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"},
            headers={"User-Agent": "ALT-Expert/0.1 (web-search; internal)"},
        )
    except Exception:
        return []
    if r.status_code >= 400:
        return []
    try:
        data: dict[str, Any] = r.json()
    except Exception:
        return []

    abs_text = str(data.get("AbstractText") or "").strip()
    abs_url = str(data.get("AbstractURL") or "").strip()
    heading = str(data.get("Heading") or "Краткая справка").strip()
    if abs_text and abs_url:
        out.append(WebSnippet(title=heading, url=_norm_url(abs_url), excerpt=_clip(_SPACE_RE.sub(" ", abs_text), 900)))

    ans = str(data.get("Answer") or "").strip()
    if ans and not out:
        out.append(
            WebSnippet(title="Краткий ответ", url="https://duckduckgo.com/", excerpt=_clip(_SPACE_RE.sub(" ", ans), 900))
        )

    for topic in (data.get("RelatedTopics") or [])[: max_snippets * 2]:
        if len(out) >= max_snippets:
            break
        if not isinstance(topic, dict):
            continue
        t = str(topic.get("Text") or "").strip()
        u = str(topic.get("FirstURL") or "").strip()
        if t and u and u.startswith("http"):
            out.append(WebSnippet(title=_clip(t, 120), url=_norm_url(u), excerpt=_clip(_SPACE_RE.sub(" ", t), 700)))

    return out[:max_snippets]


def _parse_ddg_html_results(html: str, max_snippets: int) -> list[WebSnippet]:
    out: list[WebSnippet] = []
    for m in _WEB_RESULT_BLOCK.finditer(html or ""):
        if len(out) >= max_snippets:
            break
        block = m.group(1) or ""
        hm = re.search(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"', block, re.I)
        if not hm:
            continue
        url = _norm_url(hm.group(1))
        if not url.startswith("http"):
            continue
        tm = re.search(r'<a[^>]+class="result__a"[^>]+href="[^"]+"[^>]*>(.*?)</a>', block, re.DOTALL | re.I)
        title = _strip_tags(tm.group(1)) if tm else url
        sm = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', block, re.DOTALL | re.I)
        excerpt = _clip(_strip_tags(sm.group(1)), 720) if sm else _clip(title, 360)
        if not title:
            title = url
        out.append(WebSnippet(title=title, url=url, excerpt=excerpt or title))
    return out


async def _ddg_html_lite(client: httpx.AsyncClient, query: str, max_snippets: int) -> list[WebSnippet]:
    try:
        r = await client.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query, "b": ""},
            headers={"User-Agent": "Mozilla/5.0 (compatible; ALT-Expert/0.1; +internal)"},
            follow_redirects=True,
        )
    except Exception:
        return []
    if r.status_code >= 400:
        return []
    return _parse_ddg_html_results(r.text or "", max_snippets=max_snippets)


async def search_web_snippets(
    *,
    query: str,
    timeout_s: float = 14.0,
    max_snippets: int = 6,
) -> list[WebSnippet]:
    """
    Короткие выдержки из открытого веба:
    1) DuckDuckGo Instant Answer API (JSON),
    2) если слишком мало материала — HTML-версия выдачи (осторожный парсинг блоков web-result).

    Без API-ключей; применять только как fallback после БЗ и whitelist-источников.
    """
    q = (query or "").strip()
    if len(q) > 400:
        q = q[:400]
    if len(q) < 2:
        return []

    out: list[WebSnippet] = []
    seen: set[str] = set()

    async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=True) as client:
        instant = await _ddg_instant_answer(client, q, max_snippets=max_snippets)
        for s in instant:
            key = s.url.split("?")[0].rstrip("/")
            if key in seen:
                continue
            seen.add(key)
            out.append(s)

        if len(out) < 2:
            html_hits = await _ddg_html_lite(client, q, max_snippets=max_snippets)
            for s in html_hits:
                key = s.url.split("?")[0].rstrip("/")
                if key in seen:
                    continue
                seen.add(key)
                out.append(s)
                if len(out) >= max_snippets:
                    break

    return out[:max_snippets]
