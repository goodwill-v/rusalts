from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from datetime import date, datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any

import httpx
import yaml

from app import config
from app.observability import json_log
from app.parser_agent.models import ChangeItem, FetchResult, Source


_FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_SPACE_RE = re.compile(r"\s+")
_DATE_ISO_RE = re.compile(r"\b(20\d{2}-[01]\d-[0-3]\d)\b")
_DATE_DMY_RE = re.compile(r"\b([0-3]?\d\.[01]?\d\.20\d{2})\b")
_DATE_RU_RE = re.compile(
    r"\b([0-3]?\d)\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\s+(20\d{2})\b",
    re.IGNORECASE,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _slugify(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9а-яё]+", "-", s, flags=re.IGNORECASE)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "item"


def _stable_id(prefix: str, raw: str) -> str:
    h = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]  # noqa: S324 (non-security)
    return f"{prefix}-{h}"


def _load_sources(path: Path) -> list[Source]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    items = raw.get("sources") or []
    out: list[Source] = []
    for s in items:
        if not isinstance(s, dict):
            continue
        sid = str(s.get("id") or "").strip()
        url = str(s.get("url") or "").strip()
        if not sid or not url:
            continue
        out.append(
            Source(
                id=sid,
                title=str(s.get("title") or sid).strip(),
                url=url,
                priority=str(s.get("priority") or "low").strip(),
                level=int(s.get("level") or 3),
                content_type=str(s.get("content_type") or "").strip(),
                frequency=str(s.get("frequency") or "").strip(),
                status=str(s.get("status") or "").strip(),
            )
        )
    return out


def _state_paths(source_id: str) -> tuple[Path, Path]:
    safe = _slugify(source_id)
    state_dir = config.MONITORING_DIR / "state"
    snap_dir = config.MONITORING_DIR / "snapshots"
    state_dir.mkdir(parents=True, exist_ok=True)
    snap_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / f"{safe}.json", snap_dir / f"{safe}.txt"


def _read_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _write_state(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _strip_html_to_text(html: str) -> str:
    # Remove scripts/styles early.
    html = re.sub(r"(?is)<(script|style|noscript)\b.*?>.*?</\1>", " ", html)
    # Prefer main/article blocks if present.
    m = re.search(r"(?is)<(main|article)\b.*?>.*?</\1>", html)
    if m:
        html = m.group(0)
    # Drop all tags.
    text = re.sub(r"(?is)<[^>]+>", " ", html)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


async def _fetch(source: Source, *, client: httpx.AsyncClient) -> FetchResult:
    state_path, _snap_path = _state_paths(source.id)
    st = _read_state(state_path)

    headers: dict[str, str] = {
        "User-Agent": "ALT-Parser/0.1 (+daily-monitoring; contact: internal)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    if st.get("etag"):
        headers["If-None-Match"] = str(st["etag"])
    if st.get("last_modified"):
        headers["If-Modified-Since"] = str(st["last_modified"])

    r = await client.get(source.url, headers=headers, follow_redirects=True)
    if r.status_code == 304:
        return FetchResult(
            source=source,
            status=304,
            final_url=str(r.url),
            etag=st.get("etag"),
            last_modified=st.get("last_modified"),
            content_type=r.headers.get("content-type"),
            text="",
        )

    raw_text = ""
    if r.status_code < 400:
        raw_text = _strip_html_to_text(r.text or "")

    etag = r.headers.get("etag")
    last_mod = r.headers.get("last-modified")
    return FetchResult(
        source=source,
        status=r.status_code,
        final_url=str(r.url),
        etag=str(etag).strip() if etag else None,
        last_modified=str(last_mod).strip() if last_mod else None,
        content_type=r.headers.get("content-type"),
        text=raw_text,
    )


def _classify(source: Source) -> tuple[str, str]:
    sid = source.id.lower()
    url = source.url.lower()
    ct = (source.content_type or "").lower()

    if any(x in url for x in ("rkn.gov.ru", "digital.gov.ru")) or any(x in sid for x in ("roskomnadzor", "mintsifry")):
        return "legal.regulatory", "MAX/Регуляторика_и_комплаенс/Законы_и_требования"
    if "dev.max.ru" in url or "changelog" in sid or "sdk" in ct or "api" in ct or "github" in url or "github" in sid:
        return "tech.changelog", "MAX/Разработчикам/API_и_SDK"
    if "news" in url or "новости" in ct:
        return "news.official", "Новости_и_изменения/MAX/Важные_объявления"
    return "product.general", "MAX/Обзор_платформы"


def _extract_front_matter(text: str) -> tuple[dict[str, Any], str]:
    m = _FRONT_MATTER_RE.search(text)
    if not m:
        return {}, text
    meta_raw = m.group(1)
    body = text[m.end() :]
    meta = yaml.safe_load(meta_raw) or {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, body.lstrip()


def _write_article(
    *,
    section_path: str,
    article_id: str,
    title: str,
    updated_at_utc: str,
    sources: list[dict[str, str]],
    body: str,
    legal_fields: dict[str, str | None] | None = None,
) -> Path:
    rel_dir = Path("knowledge_base") / "articles" / Path(*section_path.split("/"))
    out_dir = (config.BASE_DIR / rel_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{article_id}.md"

    meta: dict[str, Any] = {
        "id": article_id,
        "section_path": section_path,
        "title": title,
        "updated_at_utc": updated_at_utc,
        "keywords": [],
        "legal_relevance": "none",
        "sources": sources,
    }
    if legal_fields:
        for k in ("legal_relevance", "legal_status", "effective_from", "jurisdiction"):
            v = legal_fields.get(k)
            if v is not None:
                meta[k] = v

    if path.is_file():
        old = path.read_text(encoding="utf-8")
        old_meta, _old_body = _extract_front_matter(old)
        if isinstance(old_meta, dict):
            # Preserve existing keywords if any.
            kws = old_meta.get("keywords")
            if isinstance(kws, list) and kws:
                meta["keywords"] = [str(x) for x in kws if str(x).strip()]
            # Preserve legal fields if already set.
            for k in ("legal_relevance", "legal_status", "effective_from", "jurisdiction"):
                if old_meta.get(k) is not None and (not legal_fields or legal_fields.get(k) is None):
                    meta[k] = old_meta.get(k)

    fm = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
    content = f"---\n{fm}\n---\n\n{body.strip()}\n"
    path.write_text(content, encoding="utf-8")
    return path


def _append_kb_changelog(*, ts_utc: str, change: ChangeItem) -> None:
    kb_dir = config.KNOWLEDGE_BASE_DIR
    month_key = ts_utc[:7]  # YYYY-MM
    jsonl_path = kb_dir / "changelog" / f"{month_key}.jsonl"
    md_path = kb_dir / "changelog" / f"{month_key}.md"
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)

    jsonl_rec = {
        "ts": ts_utc,
        "type": "kb_update",
        "article_id": change.article_id,
        "section_path": change.section_path,
        "change_package_item_id": change.item_id,
        "summary": change.summary,
        "sources": change.links,
    }
    with jsonl_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(jsonl_rec, ensure_ascii=False) + "\n")

    day_key = ts_utc[:10]
    if not md_path.is_file():
        md_path.write_text(f"# Журнал изменений Базы знаний — {month_key}\n\n", encoding="utf-8")
    existing = md_path.read_text(encoding="utf-8")
    if f"## {day_key}\n" not in existing:
        with md_path.open("a", encoding="utf-8") as f:
            f.write(f"\n## {day_key}\n\n")
    with md_path.open("a", encoding="utf-8") as f:
        f.write(f"- {change.summary} (источник: {change.source_url})\n")


def _write_change_package(*, day: date, items: list[ChangeItem]) -> Path:
    day_dir = config.CHANGES_DIR / day.isoformat()
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / "change_package.json"
    payload = {
        "meta": {
            "generated_at_utc": _utc_now_iso(),
            "day": day.isoformat(),
            "count": len(items),
            "producer": "parser",
            "version": "0.1",
        },
        "items": [asdict(x) for x in items],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _summarize_diff(prev: str, cur: str, *, max_len: int = 300) -> str:
    if not prev:
        base = "Новый источник/первая фиксация текста."
    else:
        # Cheap heuristic: length delta + first changed window.
        d = len(cur) - len(prev)
        base = f"Обновление источника: изменение текста (Δ{d} символов)."
    return (base[: max_len - 1] + "…") if len(base) > max_len else base


def _extract_pub_date(text: str) -> str:
    """
    Извлекаем дату публикации из извлечённого текста.
    Это эвристика: для разных сайтов формат отличается.
    """
    t = _SPACE_RE.sub(" ", (text or "")).strip()
    if not t:
        return ""
    m = _DATE_ISO_RE.search(t)
    if m:
        return m.group(1)
    m = _DATE_DMY_RE.search(t)
    if m:
        return m.group(1)
    m = _DATE_RU_RE.search(t)
    if m:
        d, mon, y = m.group(1), m.group(2), m.group(3)
        return f"{int(d):02d} {mon.lower()} {y}"
    return ""


def _extract_topic(text: str, *, max_words: int = 18, max_chars: int = 140) -> str:
    """
    Пытаемся извлечь «ключевую тему» из первых строк текста.
    Если HTML не содержит явного заголовка, берём первые слова, отсекая шум.
    """
    t = _SPACE_RE.sub(" ", (text or "")).strip()
    if not t:
        return ""

    # Частые "шапки/меню" в извлечённом тексте (минимально).
    t = re.sub(r"\b(главная|новости|контакты|войти|регистрация|поиск)\b", " ", t, flags=re.IGNORECASE)
    t = _SPACE_RE.sub(" ", t).strip()

    # Срез до первого "естественного" разделителя.
    cut_pos = len(t)
    for sep in (". ", " | ", " — ", "\n"):
        p = t.find(sep)
        if 20 <= p < cut_pos:
            cut_pos = p
    head = t[:cut_pos].strip()

    words = [w for w in re.split(r"[^\w\dа-яё\-]+", head, flags=re.IGNORECASE) if w]
    if not words:
        return ""
    topic = " ".join(words[:max_words]).strip()
    if len(topic) > max_chars:
        topic = topic[: max_chars - 1].rstrip() + "…"
    return topic


def _human_summary(*, source_title: str, prev_text: str, cur_text: str) -> str:
    pub_date = _extract_pub_date(cur_text)
    topic = _extract_topic(cur_text)
    prefix = "Новая публикация" if not prev_text else "Обновление"
    date_part = pub_date or "дата не найдена"
    topic_part = topic or "тема не распознана"
    return f"{source_title}: {date_part} — {topic_part} ({prefix})"


async def run_once(*, limit: int | None = None) -> dict[str, Any]:
    """
    One-off run: fetch sources, detect changes, update KB and emit change_package.
    Intended to be executed daily at 05:00 UTC by an external scheduler (cron/systemd).
    """
    config.ensure_data_dirs()

    sources_path = config.BASE_DIR / "parser" / "sources.json"
    sources = _load_sources(sources_path)
    if limit is not None:
        sources = sources[: max(0, int(limit))]

    ts = _utc_now_iso()
    today = datetime.now(timezone.utc).date()

    changed_items: list[ChangeItem] = []
    fetched = 0
    changed = 0

    async with httpx.AsyncClient(timeout=25.0) as client:
        for src in sources:
            fetched += 1
            state_path, snap_path = _state_paths(src.id)
            st = _read_state(state_path)

            try:
                fr = await _fetch(src, client=client)
            except Exception as e:  # noqa: BLE001
                json_log({"type": "parser_fetch_failed", "source_id": src.id, "url": src.url, "error": str(e)})
                continue

            if fr.status == 304:
                continue
            if fr.status >= 400 or not fr.text:
                json_log(
                    {
                        "type": "parser_fetch_http_error",
                        "source_id": src.id,
                        "url": src.url,
                        "status": fr.status,
                    }
                )
                continue

            prev_text = snap_path.read_text(encoding="utf-8") if snap_path.is_file() else ""
            cur_text = fr.text.strip()
            if prev_text.strip() == cur_text:
                # Even if headers changed, content did not.
                continue

            classification, section_path = _classify(src)
            article_id = _stable_id("src", src.id)
            title = f"{src.title} — мониторинг"

            legal_fields: dict[str, str | None] | None = None
            if classification.startswith("legal."):
                legal_fields = {
                    "legal_relevance": "high" if src.priority == "high" else "medium",
                    "legal_status": "unknown",
                    "effective_from": None,
                    "jurisdiction": "РФ",
                }

            body = (
                f"Источник: {src.title}\n\n"
                f"- URL: {fr.final_url}\n"
                f"- Обновлено: {ts}\n\n"
                "Извлечённый текст (автоматически):\n\n"
                f"{cur_text[:20000]}\n"
            )
            article_path = _write_article(
                section_path=section_path,
                article_id=article_id,
                title=title,
                updated_at_utc=ts,
                sources=[{"title": src.title, "url": src.url}],
                body=body,
                legal_fields=legal_fields,
            )

            # Более полезное для новостей резюме: источник + дата (если найдена) + тема.
            # При необходимости технический Δ сохраняется в KB-чейнджлог/статьях.
            summary = _human_summary(source_title=src.title, prev_text=prev_text, cur_text=cur_text)
            item = ChangeItem(
                item_id=_stable_id("chg", f"{src.id}:{ts}"),
                ts_utc=ts,
                source_id=src.id,
                source_title=src.title,
                source_url=src.url,
                classification=classification,
                section_path=section_path,
                article_id=article_id,
                article_path=str(article_path),
                summary=summary,
                links=[src.url],
            )
            _append_kb_changelog(ts_utc=ts, change=item)
            changed_items.append(item)

            snap_path.write_text(cur_text + "\n", encoding="utf-8")
            _write_state(
                state_path,
                {
                    "source_id": src.id,
                    "url": src.url,
                    "last_run_utc": ts,
                    "etag": fr.etag,
                    "last_modified": fr.last_modified,
                    "content_type": fr.content_type,
                    "final_url": fr.final_url,
                    "last_status": fr.status,
                    "prev_snapshot_len": len(prev_text),
                    "snapshot_len": len(cur_text),
                    "prev_snapshot_sha1": hashlib.sha1(prev_text.encode("utf-8")).hexdigest() if prev_text else None,  # noqa: S324
                    "snapshot_sha1": hashlib.sha1(cur_text.encode("utf-8")).hexdigest(),  # noqa: S324
                },
            )
            changed += 1

    pkg_path = _write_change_package(day=today, items=changed_items)
    json_log(
        {
            "type": "parser_run_complete",
            "ts_utc": ts,
            "sources_total": len(sources),
            "fetched": fetched,
            "changed": changed,
            "change_package_path": str(pkg_path),
        }
    )
    return {
        "ok": True,
        "ts_utc": ts,
        "sources_total": len(sources),
        "fetched": fetched,
        "changed": changed,
        "change_package_path": str(pkg_path),
        "items": [asdict(x) for x in changed_items],
    }

