"""Чтение новостных RSS/Atom-лент для ежедневного дайджеста АЛТ.

Реестр источников: parser/news_sources.json (см. ТЗ «Автоконтент v2», п. 2.1).
Зависимости: только stdlib (xml.etree) + httpx — новых пакетов не добавляем.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

from app import config
from app.observability import json_log

USER_AGENT = "ALT-NewsFeeds/1.0 (+https://rusalts.ru; daily digest)"

_TAG_RE = re.compile(r"(?is)<[^>]+>")
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class NewsSource:
    """Источник из реестра parser/news_sources.json."""

    id: str
    title: str
    url: str
    type: str = "rss"  # rss | diff
    lang: str = "ru"
    origin: str = "ru"  # ru | world — отечественный приоритет при скоринге
    priority: str = "medium"
    enabled: bool = True
    max_items: int = 6
    fresh_window_hours: int = 48
    topics: list[str] = field(default_factory=list)
    include_any: list[str] = field(default_factory=list)
    exclude_any: list[str] = field(default_factory=list)
    site_url: str = ""
    comment: str = ""


@dataclass(frozen=True)
class NewsItem:
    """Кандидат в дайджест: один заголовок/новость из ленты."""

    uid: str
    source_id: str
    source_title: str
    title: str
    url: str
    summary: str
    published: str  # ISO-подобная строка или ""
    lang: str
    origin: str
    fetched_at_utc: str
    topics: list[str] = field(default_factory=list)


def load_news_sources(path: Path | None = None) -> list[NewsSource]:
    path = path or (config.BASE_DIR / "parser" / "news_sources.json")
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        json_log({"type": "news_sources_load_failed", "path": str(path), "error": str(e)})
        return []
    out: list[NewsSource] = []
    for s in raw.get("sources") or []:
        if not isinstance(s, dict):
            continue
        sid = str(s.get("id") or "").strip()
        url = str(s.get("url") or "").strip()
        if not sid or not url:
            continue
        if not bool(s.get("enabled", True)):
            continue
        out.append(
            NewsSource(
                id=sid,
                title=str(s.get("title") or sid).strip(),
                url=url,
                type=str(s.get("type") or "rss").strip().lower(),
                lang=str(s.get("lang") or "ru").strip().lower(),
                origin=str(s.get("origin") or ("ru" if str(s.get("lang") or "ru") == "ru" else "world")).strip().lower(),
                priority=str(s.get("priority") or "medium").strip().lower(),
                enabled=bool(s.get("enabled", True)),
                max_items=int(s.get("max_items") or 6),
                fresh_window_hours=int(s.get("fresh_window_hours") or 48),
                topics=[str(t).strip() for t in (s.get("topics") or []) if str(t).strip()],
                include_any=[str(t).strip().lower() for t in (s.get("include_any") or []) if str(t).strip()],
                exclude_any=[str(t).strip().lower() for t in (s.get("exclude_any") or []) if str(t).strip()],
                site_url=str(s.get("site_url") or "").strip(),
                comment=str(s.get("comment") or "").strip(),
            )
        )
    return out


def load_topic_config(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Темы п. 2.5 ТЗ: порядок приоритета + ключевые слова."""
    path = path or (config.BASE_DIR / "parser" / "news_sources.json")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        topics = raw.get("topics") or {}
        return {k: v for k, v in topics.items() if isinstance(v, dict)} if isinstance(topics, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------------------
# URL-нормализация (дедуп по ссылке)
# ---------------------------------------------------------------------------

_UTM_PREFIXES = ("utm_", "yclid", "gclid", "fbclid", "from", "ref", "referrer")


def keyword_hit(text: str, keyword: str) -> bool:
    """Сравнение с ключевым словом.

    Короткие ключи (< 5 символов: «ии», «gpt», «макс») — только как отдельное слово
    (\\b-границы), иначе «ии» ловится внутри «России». Длинные ключи — это в основном
    основы слов («автоматизац», «нейросет») — ищем подстрокой.
    """
    kw = str(keyword or "").strip().lower()
    if not kw:
        return False
    t = str(text or "").lower()
    if len(kw) >= 5:
        return kw in t
    return re.search(rf"(?<![\w-]){re.escape(kw)}(?![\w-])", t) is not None


def keywords_any_hit(text: str, keywords: list[str]) -> bool:
    return any(keyword_hit(text, kw) for kw in keywords or [])


def keywords_hits_count(text: str, keywords: list[str]) -> int:
    return sum(1 for kw in keywords or [] if keyword_hit(text, kw))


def canonical_url(url: str) -> str:
    """Убираем трекинг-параметры и фрагмент; приводим схему/host к каноническому виду."""
    u = str(url or "").strip()
    if not u:
        return ""
    if not re.match(r"^https?://", u, re.IGNORECASE):
        return u.lower()
    try:
        p = urlparse(u)
    except Exception:  # noqa: BLE001
        return u.lower()
    host = (p.hostname or "").lower().removeprefix("www.")
    query = [(k, v) for k, v in parse_qsl(p.query or "", keep_blank_values=True) if not k.lower().startswith(_UTM_PREFIXES)]
    path = p.path or "/"
    return urlunparse(("https", host, path, "", urlencode(query), "")).rstrip("/")


# ---------------------------------------------------------------------------
# Парсинг RSS 2.0 / Atom (stdlib)
# ---------------------------------------------------------------------------


def _strip_html(s: str) -> str:
    text = _TAG_RE.sub(" ", unescape(str(s or "")))
    return _WS_RE.sub(" ", text).strip()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _parse_date(value: str) -> str:
    """RFC822/ISO → ISO-строка. Возвращает '' если не разобралось."""
    v = str(value or "").strip()
    if not v:
        return ""
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except Exception:
        pass
    try:
        v2 = v.replace("Z", "+00:00")
        dt = datetime.fromisoformat(v2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except Exception:
        return ""


def parse_feed(xml_text: str) -> list[dict[str, str]]:
    """Минимальный парсер RSS 2.0 / Atom. Возвращает список dict(title, url, summary, published)."""
    import xml.etree.ElementTree as ET

    root = ET.fromstring(str(xml_text or ""))
    is_atom = _local(root.tag) == "feed"
    out: list[dict[str, str]] = []

    def _collect(parent: Any) -> dict[str, str]:
        d: dict[str, Any] = {"title": "", "url": "", "summary": "", "published": ""}
        for ch in parent:
            t = _local(ch.tag)
            if t == "title":
                d["title"] = _strip_html(ch.text or "")
            elif t == "link":
                if is_atom:
                    rel = (ch.attrib or {}).get("rel", "alternate")
                    if rel == "alternate" or not (ch.attrib or {}).get("href"):
                        href = (ch.attrib or {}).get("href") or (ch.text or "")
                        if href:
                            d["url"] = href.strip()
                else:
                    d["url"] = (ch.text or "").strip()
            elif t in ("description", "summary", "content"):
                if not d["summary"]:
                    d["summary"] = _strip_html(ch.text or "")
            elif t in ("pubdate", "published", "updated", "date"):
                if not d["published"]:
                    d["published"] = ch.text or ""
        return d

    children = list(root) if is_atom else []
    if not is_atom:
        for ch in root:
            if _local(ch.tag) == "channel":
                children = list(ch)
                break
    for el in children:
        t = _local(el.tag)
        if t == "item" or t == "entry":
            rec = _collect(el)
            if rec.get("title") or rec.get("url"):
                out.append(rec)
    return out


def _uid_for(source_id: str, url: str, title: str) -> str:
    import hashlib

    h = hashlib.sha1(f"{source_id}|{canonical_url(url) or title}".encode("utf-8")).hexdigest()[:12]  # noqa: S324
    return f"news-{h}"


def _matches_filters(item: dict[str, str], src: NewsSource) -> bool:
    blob = f"{item.get('title', '')} {item.get('summary', '')}"
    if keywords_any_hit(blob, src.exclude_any):
        return False
    if src.include_any:
        return keywords_any_hit(blob, src.include_any)
    return True


def _fresh_enough(published_iso: str, window_hours: int, now: datetime | None = None) -> bool:
    """Нет даты — пропускаем (лента сама отдаёт свежее); старше окна — отбрасываем."""
    if not published_iso:
        return True
    now = now or datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(published_iso.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - dt) <= timedelta(hours=max(1, window_hours))


def news_items_from_feed(source: NewsSource, xml_text: str, *, now: datetime | None = None) -> list[NewsItem]:
    """XML → отфильтрованные NewsItem (фильтры реестра: include/exclude/свежесть/лимит)."""
    fetched_at = (now or datetime.now(timezone.utc)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    out: list[NewsItem] = []
    try:
        records = parse_feed(xml_text)
    except Exception as e:  # noqa: BLE001
        json_log({"type": "news_feed_parse_failed", "source_id": source.id, "error": str(e)})
        return []
    for rec in records:
        title = rec.get("title", "").strip()
        url = rec.get("url", "").strip()
        if not title or not url:
            continue
        if not _matches_filters(rec, source):
            continue
        published = _parse_date(rec.get("published", ""))
        if not _fresh_enough(published, source.fresh_window_hours, now=now):
            continue
        summary = rec.get("summary", "").strip()
        if len(summary) > 400:
            summary = summary[:399].rstrip() + "…"
        out.append(
            NewsItem(
                uid=_uid_for(source.id, url, title),
                source_id=source.id,
                source_title=source.title,
                title=title,
                url=url,
                summary=summary,
                published=published,
                lang=source.lang,
                origin=source.origin,
                fetched_at_utc=fetched_at,
                topics=list(source.topics),
            )
        )
        if len(out) >= source.max_items:
            break
    return out


async def fetch_source_feed(source: NewsSource, *, client: httpx.AsyncClient) -> list[NewsItem]:
    """Забрать одну RSS-ленту. Ошибки логируются и не роняют общий цикл."""
    if source.type != "rss":
        return []
    try:
        r = await client.get(source.url)
    except Exception as e:  # noqa: BLE001
        json_log({"type": "news_feed_fetch_failed", "source_id": source.id, "url": source.url, "error": str(e)})
        return []
    if r.status_code >= 400:
        json_log({"type": "news_feed_http_error", "source_id": source.id, "url": source.url, "status": r.status_code})
        return []
    items = news_items_from_feed(source, r.text)
    json_log({"type": "news_feed_fetched", "source_id": source.id, "items": len(items)})
    return items


async def fetch_all_feeds(*, sources: list[NewsSource] | None = None) -> list[NewsItem]:
    """Все RSS-источники реестра → список кандидатов (без дедупа — им занимается content_digest)."""
    srcs = [s for s in (sources if sources is not None else load_news_sources()) if s.enabled and s.type == "rss"]
    out: list[NewsItem] = []
    timeout = httpx.Timeout(20.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        for src in srcs:
            out.extend(await fetch_source_feed(src, client=client))
    return out


def change_items_to_news(items: list[dict], *, now_utc: str = "") -> list[NewsItem]:
    """Кандидаты из старого change-пакета (diff-мониторинг) → NewsItem для единого дайджеста.

    Так TAdviser/РБК Тренды (RSS закрыт) участвуют в дайджесте наравне с RSS-лентами.
    """
    topics_by_source = {s.id: s for s in load_news_sources()}
    fetched_at = now_utc or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    out: list[NewsItem] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        sid = str(it.get("source_id") or "").strip()
        src = topics_by_source.get(sid)
        url = str(it.get("source_url") or "").strip()
        summary = str(it.get("summary") or "").strip()
        if src:
            url = url or src.url
            origin, lang, topics, source_title = src.origin, src.lang, list(src.topics), src.title
        else:
            origin = "ru" if ".ru" in url else "world"
            lang = "ru"
            topics = []
            source_title = str(it.get("source_title") or sid or "источник")
        out.append(
            NewsItem(
                uid=_uid_for(sid or "chg", url, summary),
                source_id=sid or "change",
                source_title=source_title,
                title=summary[:200],
                url=url,
                summary=summary[:400],
                published=str(it.get("ts_utc") or ""),
                lang=lang,
                origin=origin,
                fetched_at_utc=fetched_at,
                topics=topics,
            )
        )
    return out


def news_item_to_dict(item: NewsItem) -> dict:
    return asdict(item)
