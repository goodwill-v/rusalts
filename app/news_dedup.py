"""Дедупликация фактов против опубликованного + правило МАКС (ТЗ «Автоконтент v2», пп. 2.3, 4).

Дедуп двухуровневый:
1. URL — канонический адрес уже виделся (трекинг-параметры не считаются);
2. Факт/тема — шинглы (словесные n-граммы) заголовка+анонса сравниваются с опубликованным
   за период и с уже отдайджестенным; похожесть выше порога = повтор.

Правило МАКС: новости про мессенджер МАКС не выходят отдельными выпусками — копятся
в пуле и выходят одним еженедельным релизом-сводкой (пятница).

Состояние хранится в data/content/news_digest_state.json (runtime, вне git).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app import config
from app.news_feeds import NewsItem, canonical_url

# Порог похожести шинглов, выше которого считаем «это уже опубликованный факт».
SIMILARITY_THRESHOLD = 0.55

# Период сравнения с опубликованным на сайте (дни).
PUBLISHED_WINDOW_DAYS = 14

# Сколько дней помнить уже отдайджестенные факты.
SEEN_FACTS_TTL_DAYS = 30

_WS = re.compile(r"[^\wа-яё]+", re.IGNORECASE)


def _tokens(text: str) -> list[str]:
    return [t for t in _WS.split((text or "").lower()) if len(t) > 2]


def shingles(text: str, *, size: int = 3) -> set[tuple[str, ...]]:
    """Словесные n-граммы — основа сравнения «похожести фактов», устойчива к перефразированию."""
    toks = _tokens(text)
    if len(toks) < size:
        return {tuple(toks)} if toks else set()
    return {tuple(toks[i : i + size]) for i in range(len(toks) - size + 1)}


def similarity(text_a: str, text_b: str) -> float:
    """Коэффициент Жаккара по шинглам: 1.0 — совпадение, 0.0 — ничего общего."""
    a, b = shingles(text_a), shingles(text_b)
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / len(a | b)


def fact_hash(title: str, summary: str) -> str:
    """Быстрый ключ «факта» — отсортированные значимые слова (для точного повтора)."""
    toks = sorted(set(_tokens(f"{title} {summary}")))
    return hashlib.sha1(" ".join(toks).encode("utf-8")).hexdigest()[:16]  # noqa: S324


@dataclass
class KnownFact:
    title: str
    summary: str = ""
    url: str = ""
    ts_utc: str = ""
    fact_hash: str = ""

    def blob(self) -> str:
        return f"{self.title} {self.summary}".strip()


@dataclass
class DedupVerdict:
    duplicate: bool
    reason: str = ""  # url | fact_hash | similar
    matched_title: str = ""


@dataclass
class DigestState:
    """Персистентное состояние дайджеста (data/content/news_digest_state.json)."""

    seen_facts: list[dict[str, Any]] = field(default_factory=list)
    seen_urls: dict[str, str] = field(default_factory=dict)  # canonical_url → first seen ts
    max_pool: list[dict[str, Any]] = field(default_factory=list)  # накопленные МАКС-новости
    last_digest_utc: str = ""
    last_max_release_utc: str = ""
    version: int = 1


def _state_path() -> Path:
    return config.DATA_DIR / "content" / "news_digest_state.json"


def load_digest_state() -> DigestState:
    path = _state_path()
    if not path.is_file():
        return DigestState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return DigestState()
    if not isinstance(raw, dict):
        return DigestState()
    return DigestState(
        seen_facts=[x for x in (raw.get("seen_facts") or []) if isinstance(x, dict)],
        seen_urls={str(k): str(v) for k, v in (raw.get("seen_urls") or {}).items() if k and v},
        max_pool=[x for x in (raw.get("max_pool") or []) if isinstance(x, dict)],
        last_digest_utc=str(raw.get("last_digest_utc") or ""),
        last_max_release_utc=str(raw.get("last_max_release_utc") or ""),
        version=int(raw.get("version") or 1),
    )


def save_digest_state(state: DigestState) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")


def _parse_iso(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return None


class FactDedup:
    """Проверка кандидата против: опубликованного на сайте + уже отдайджестенного."""

    def __init__(
        self,
        *,
        published: list[dict[str, Any]] | None = None,
        state: DigestState | None = None,
        now: datetime | None = None,
        sim_threshold: float = SIMILARITY_THRESHOLD,
        published_window_days: int = PUBLISHED_WINDOW_DAYS,
    ) -> None:
        self.now = now or datetime.now(timezone.utc)
        self.sim_threshold = sim_threshold
        self.window = timedelta(days=published_window_days)
        self.state = state or DigestState()
        self._url_set: set[str] = set(self.state.seen_urls.keys())
        self._published: list[KnownFact] = []
        for row in published or []:
            ts = _parse_iso(str(row.get("published_at_utc") or ""))
            if ts and (self.now - ts) > self.window:
                continue  # старше периода сравнения — не мешает
            self._published.append(
                KnownFact(
                    title=str(row.get("title") or ""),
                    summary=str(row.get("excerpt") or ""),
                    url=str(row.get("url") or ""),
                    ts_utc=str(row.get("published_at_utc") or ""),
                )
            )
        cutoff = self.now - timedelta(days=SEEN_FACTS_TTL_DAYS)
        self._seen: list[KnownFact] = []
        for row in self.state.seen_facts:
            ts = _parse_iso(str(row.get("ts_utc") or ""))
            if ts and ts < cutoff:
                continue
            self._seen.append(
                KnownFact(
                    title=str(row.get("title") or ""),
                    summary=str(row.get("summary") or ""),
                    url=str(row.get("url") or ""),
                    ts_utc=str(row.get("ts_utc") or ""),
                    fact_hash=str(row.get("fact_hash") or ""),
                )
            )

    def check(self, *, title: str, summary: str, url: str = "") -> DedupVerdict:
        """Дубликат ли: сначала URL, затем хеш факта, затем похожесть шинглов."""
        cu = canonical_url(url)
        if cu and cu in self._url_set:
            return DedupVerdict(True, "url", url)

        fh = fact_hash(title, summary)
        blob = f"{title} {summary}".strip()
        for known in self._published + self._seen:
            if known.fact_hash and known.fact_hash == fh:
                return DedupVerdict(True, "fact_hash", known.title)
        for known in self._published + self._seen:
            if similarity(blob, known.blob()) >= self.sim_threshold:
                return DedupVerdict(True, "similar", known.title)
        return DedupVerdict(False)

    def remember(self, *, title: str, summary: str, url: str) -> None:
        """Запомнить факт как опубликованный/использованный (вызывать после создания item)."""
        cu = canonical_url(url)
        if cu:
            self.state.seen_urls[cu] = self.now.replace(microsecond=0).isoformat()
            self._url_set.add(cu)
        rec = {
            "ts_utc": self.now.replace(microsecond=0).isoformat(),
            "title": title,
            "summary": (summary or "")[:400],
            "url": url,
            "fact_hash": fact_hash(title, summary),
        }
        self.state.seen_facts.append(rec)
        # Тот же факт — в память текущего инстанса (проверки после remember в этом же запуске)
        self._seen.append(
            KnownFact(
                title=title,
                summary=(summary or "")[:400],
                url=url,
                ts_utc=rec["ts_utc"],
                fact_hash=rec["fact_hash"],
            )
        )


def news_item_to_pool_record(item: NewsItem) -> dict[str, Any]:
    """NewsItem → компактная запись пула МАКС (для хранения между запусками)."""
    return {
        "uid": item.uid,
        "source_id": item.source_id,
        "source_title": item.source_title,
        "title": item.title,
        "url": item.url,
        "summary": item.summary,
        "published": item.published,
        "fetched_at_utc": item.fetched_at_utc,
        "topics": list(item.topics),
    }


def pool_record_to_news_item(rec: dict[str, Any]) -> NewsItem:
    return NewsItem(
        uid=str(rec.get("uid") or ""),
        source_id=str(rec.get("source_id") or "max"),
        source_title=str(rec.get("source_title") or "источник"),
        title=str(rec.get("title") or ""),
        url=str(rec.get("url") or ""),
        summary=str(rec.get("summary") or ""),
        published=str(rec.get("published") or ""),
        lang="ru",
        origin="ru",
        fetched_at_utc=str(rec.get("fetched_at_utc") or ""),
        topics=[str(t) for t in (rec.get("topics") or [])],
    )


def add_to_max_pool(state: DigestState, items: list[NewsItem]) -> int:
    """Положить МАКС-новости в пул (дедуп по uid/url внутри пула). Возвращает число добавленных."""
    have: set[str] = {canonical_url(str(r.get("url") or "")) for r in state.max_pool}
    have_uid = {str(r.get("uid") or "") for r in state.max_pool}
    added = 0
    for it in items:
        cu = canonical_url(it.url)
        if it.uid in have_uid or (cu and cu in have):
            continue
        state.max_pool.append(news_item_to_pool_record(it))
        have.add(cu)
        have_uid.add(it.uid)
        added += 1
    return added


def max_weekly_due(state: DigestState, *, now: datetime | None = None) -> bool:
    """Сводка МАКС выходит в пятницу (ТЗ 2.3: «в пятницу(?)»); не чаще раза в день."""
    now = now or datetime.now(timezone.utc)
    if not state.max_pool:
        return False
    if now.weekday() != 4:  # 4 = пятница
        return False
    last = _parse_iso(state.last_max_release_utc)
    if last and last.date() == now.date():
        return False
    return True
