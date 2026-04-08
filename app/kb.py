from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


_FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass(frozen=True)
class KbSource:
    title: str
    url: str


@dataclass(frozen=True)
class KbArticle:
    id: str
    section_path: str
    title: str
    updated_at_utc: str
    keywords: list[str]
    sources: list[KbSource]
    body: str
    file_path: str
    legal_relevance: str | None = None
    legal_status: str | None = None
    effective_from: str | None = None
    jurisdiction: str | None = None


def _extract_front_matter(text: str) -> tuple[dict[str, Any], str]:
    m = _FRONT_MATTER_RE.search(text)
    if not m:
        return {}, text
    meta_raw = m.group(1)
    body = text[m.end() :]
    meta = yaml.safe_load(meta_raw) or {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, body.strip()


def load_articles(articles_dir: Path) -> list[KbArticle]:
    items: list[KbArticle] = []
    for path in sorted(articles_dir.rglob("*.md")):
        raw = path.read_text(encoding="utf-8")
        meta, body = _extract_front_matter(raw)
        article_id = str(meta.get("id") or path.stem)
        section_path = str(meta.get("section_path") or "")
        title = str(meta.get("title") or path.stem)
        updated_at_utc = str(meta.get("updated_at_utc") or "")
        keywords = meta.get("keywords") or []
        if not isinstance(keywords, list):
            keywords = []
        keywords = [str(k) for k in keywords if str(k).strip()]
        sources_meta = meta.get("sources") or []
        sources: list[KbSource] = []
        if isinstance(sources_meta, list):
            for s in sources_meta:
                if isinstance(s, dict):
                    st = str(s.get("title") or "").strip()
                    su = str(s.get("url") or "").strip()
                    if st and su:
                        sources.append(KbSource(title=st, url=su))
        items.append(
            KbArticle(
                id=article_id,
                section_path=section_path,
                title=title,
                updated_at_utc=updated_at_utc,
                keywords=keywords,
                sources=sources,
                body=body,
                file_path=str(path),
                legal_relevance=(str(meta.get("legal_relevance")) if meta.get("legal_relevance") is not None else None),
                legal_status=(str(meta.get("legal_status")) if meta.get("legal_status") is not None else None),
                effective_from=(str(meta.get("effective_from")) if meta.get("effective_from") is not None else None),
                jurisdiction=(str(meta.get("jurisdiction")) if meta.get("jurisdiction") is not None else None),
            )
        )
    return items


def _normalize(text: str) -> str:
    text = text.casefold()
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _score_article(article: KbArticle, query_terms: list[str]) -> float:
    hay_title = _normalize(article.title)
    hay_kw = _normalize(" ".join(article.keywords))
    hay_body = _normalize(article.body[:5000])

    score = 0.0
    for t in query_terms:
        if not t:
            continue
        if t in hay_title:
            score += 6.0
        if t in hay_kw:
            score += 4.0
        if t in hay_body:
            score += 2.0

    if article.section_path.startswith("MAX/Регуляторика_и_комплаенс"):
        score += 3.0
        if (article.legal_relevance or "").strip().lower() == "high":
            score += 1.0
    return score


def _excerpt(body: str, query_terms: list[str], max_len: int = 360) -> str:
    clean = re.sub(r"\s+", " ", body).strip()
    if not clean:
        return ""
    n = _normalize(clean)
    pos = None
    for t in query_terms:
        i = n.find(t)
        if i >= 0:
            pos = i
            break
    if pos is None:
        return (clean[: max_len - 1] + "…") if len(clean) > max_len else clean
    start = max(pos - 80, 0)
    end = min(start + max_len, len(clean))
    chunk = clean[start:end]
    if start > 0:
        chunk = "…" + chunk
    if end < len(clean):
        chunk = chunk + "…"
    return chunk


def search(articles: list[KbArticle], query: str, limit: int = 5) -> list[dict[str, Any]]:
    q = _normalize(query)
    terms = [t for t in re.split(r"[^\w\d\-а-яё]+", q, flags=re.IGNORECASE) if t]
    if not terms:
        return []

    scored: list[tuple[float, KbArticle]] = []
    for a in articles:
        s = _score_article(a, terms)
        if s > 0:
            scored.append((s, a))
    scored.sort(key=lambda x: x[0], reverse=True)

    out: list[dict[str, Any]] = []
    for s, a in scored[:limit]:
        out.append(
            {
                "score": round(s, 3),
                "id": a.id,
                "title": a.title,
                "section_path": a.section_path,
                "updated_at_utc": a.updated_at_utc,
                "excerpt": _excerpt(a.body, terms),
                "sources": [{"title": src.title, "url": src.url} for src in a.sources],
                "file_path": a.file_path,
                "legal_relevance": a.legal_relevance,
                "legal_status": a.legal_status,
                "effective_from": a.effective_from,
                "jurisdiction": a.jurisdiction,
            }
        )
    return out

