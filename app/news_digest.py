"""Ежедневный новостной дайджест АЛТ (ТЗ «Автоконтент v2», пп. 2.2–2.6, 4.4–4.6).

Конвейер: RSS/Atom-ленты + diff-кандидаты → дедуп фактов против опубликованного →
скоринг тем (приоритеты Шефа) → правило МАКС (накопление в пул, еженедельный релиз) →
генерация текста через RouterAI (живой тон по референсам, лид 180–220 знаков
ОТДЕЛЬНОЙ генерацией с проверкой длины) → item в очередь одобрения.

По умолчанию НИЧЕГО не публикует: item создаётся со статусом pending (очередь
/publapprov/). Автопубликация — только явным флагом auto_publish в payload.

Чистые (без I/O и сети) функции вынесены отдельно и покрыты unit-тестами:
validate_lead, fit_lead, assemble_site_text, assemble_vk_text, extract_blocks.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app import config
from app.content_store import ContentItem, next_publication_id, save_item
from app.markdown_plain import strip_markdown_public
from app.news_dedup import (
    FactDedup,
    add_to_max_pool,
    load_digest_state,
    max_weekly_due,
    pool_record_to_news_item,
    save_digest_state,
)
from app.news_feeds import (
    NewsItem,
    change_items_to_news,
    fetch_all_feeds,
    load_news_sources,
    load_topic_config,
)
from app.news_scoring import (
    ScoredCandidate,
    rank_candidates,
    score_candidate,
    select_for_digest,
)
from app.observability import json_log
from app.routerai import chat_completion
from app.model_routing import content_choice

# Размер дайджеста (ТЗ 5.1: 3–6 новостей).
DIGEST_MIN_NEWS = 3
DIGEST_MAX_NEWS = 6

# Отдельные выпуски — только при высокой значимости (ТЗ 2.4), не более 2 в день.
STANDALONE_MAX_PER_DAY = 2

# Лид-анонс: генерируем 180–200, принимаем 180–220 (ТЗ 2.6).
LEAD_MIN_CHARS = 180
LEAD_MAX_CHARS = 220
LEAD_TARGET_CHARS = 200

# Защита от двойного дайджеста в один день (systemd-таймер + ручные запуски).
DIGEST_COOLDOWN_HOURS = 20

_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)
_WS = re.compile(r"\s+")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Чистые помощники (unit-тестируемые, без I/O)
# ---------------------------------------------------------------------------


def validate_lead(lead: str, *, lo: int = LEAD_MIN_CHARS, hi: int = LEAD_MAX_CHARS) -> bool:
    """Лид-анонс должен попадать в диапазон 180–220 знаков (ТЗ 2.6)."""
    return lo <= len((lead or "").strip()) <= hi


def fit_lead(text: str, *, lo: int = LEAD_MIN_CHARS, hi: int = LEAD_MAX_CHARS) -> str:
    """Детерминированный резерв: подогнать текст под диапазон лида.

    Длинный — режем по границе слова/предложения; короткий — дотягиваем
    продолжением исходного текста. Последняя инстанция, если LLM не справился.
    """
    t = _WS.sub(" ", (text or "")).strip()
    if not t:
        return t
    if len(t) > hi:
        cut = t[:hi]
        # Граница предложения, затем слова — чтобы не рвать посреди слова.
        for sep in (". ", "! ", "? ", " — ", " "):
            p = cut.rfind(sep)
            if p >= lo - 2:
                cut = cut[: p + (0 if sep == " " else 1)]
                break
        t = cut.rstrip(" ,;:-—") + "…"
        if len(t) > hi:  # запасной точный резак
            t = t[: hi - 1].rstrip() + "…"
    if len(t) < lo:
        # Дотягиваем нейтральными связками до нижней границы (резерв-сценарий).
        fillers = [
            " Что это значит для вашего бизнеса — разбираемся в обзоре.",
            " Внутри: конкретика, цифры и практические выводы для компаний.",
            " Ссылки на первоисточники — в конце выпуска.",
        ]
        for f in fillers:
            if len(t) >= lo:
                break
            t = (t + f)[:hi] if len(t) + len(f) > hi else t + f
        if len(t) < lo:
            t = (t + "…")[:hi]
    return t


def domain_label(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        host = ""
    return host.removeprefix("www.") or "источник"


def source_lines(urls: list[str]) -> list[str]:
    """Строки «домен — https://…» в конце текста: фронт превращает их в ссылки
    с target=_blank rel=noopener (app/static/js/app.js → extractNewsSources)."""
    out: list[str] = []
    seen: set[str] = set()
    for u in urls:
        u = str(u or "").strip()
        if not u or u in seen:
            continue
        seen.add(u)
        out.append(f"{domain_label(u)} — {u}")
    return out


@dataclass
class DigestBlock:
    """Один блок-новость внутри выпуска: заголовок, суть, «что для бизнеса», ссылка."""

    headline: str
    body: str
    business: str
    url: str = ""
    candidate: ScoredCandidate | None = None


def assemble_site_text(lead: str, blocks: list[DigestBlock], urls: list[str]) -> str:
    """Вёрстка выпуска для сайта: лид → нумерованные блоки → строки источников.

    Абзацы разделяются пустой строкой — фронт рендерит каждый как <p>,
    хвостовые строки «домен — url» превращает в блок ссылок (новое окно).
    """
    parts: list[str] = [(lead or "").strip()]
    for i, b in enumerate(blocks, start=1):
        block = [f"{i}. {b.headline}".strip()]
        if b.body:
            block.append(b.body.strip())
        if b.business:
            block.append(f"Что для бизнеса: {b.business.strip()}")
        parts.append("\n".join(p for p in block if p))
    lines = source_lines(urls)
    if lines:
        parts.append("\n".join(lines))
    return "\n\n".join(p for p in parts if p).strip()


def assemble_vk_text(lead: str, blocks: list[DigestBlock], *, hashtags: str = "#АЛТ #автоматизация") -> str:
    """Версия для ВК: короче, живее, эмодзи, призыв (тон согласован с content_worker)."""
    lines = [f"📋 {_WS.sub(' ', (lead or '')).strip()}", ""]
    for i, b in enumerate(blocks, start=1):
        h = _WS.sub(" ", b.headline).strip().rstrip(".")
        lines.append(f"{i}) {h} 🔹")
    lines += ["", "Подробнее и ссылки на первоисточники — в новости на сайте 👉 rusalts.ru/news", hashtags]
    return "\n".join(lines).strip()


def extract_blocks(raw_obj: dict, candidates: list[ScoredCandidate], *, max_blocks: int) -> list[DigestBlock]:
    """JSON от LLM → блоки, привязанные к кандидатам по порядку/по полю id."""
    raw_blocks = raw_obj.get("blocks")
    if not isinstance(raw_blocks, list):
        return []
    out: list[DigestBlock] = []
    by_id = {c.uid: c for c in candidates}
    for rb in raw_blocks:
        if not isinstance(rb, dict):
            continue
        cid = str(rb.get("id") or "").strip()
        idx = len(out)
        cand = by_id.get(cid)
        if cand is None and idx < len(candidates):
            cand = candidates[idx]
        if cand is None:
            continue
        headline = _WS.sub(" ", str(rb.get("headline") or "")).strip()
        body = _WS.sub(" ", str(rb.get("body") or "")).strip()
        business = _WS.sub(" ", str(rb.get("business") or "")).strip()
        if not headline:
            headline = cand.item.title[:140]
        if not body:
            body = cand.item.summary[:400] or headline
        out.append(DigestBlock(headline=headline, body=body, business=business, url=cand.item.url, candidate=cand))
        if len(out) >= max_blocks:
            break
    return out


def _extract_json_object(text: str) -> dict:
    """LLM иногда оборачивает JSON в ```json или пояснения — достаём объект."""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty llm output")
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    for m in _FENCED_JSON_RE.finditer(raw):
        try:
            obj = json.loads((m.group(1) or "").strip())
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    i, j = raw.find("{"), raw.rfind("}")
    if 0 <= i < j:
        try:
            obj = json.loads(raw[i : j + 1])
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    raise ValueError("could not extract JSON object")


# ---------------------------------------------------------------------------
# LLM-генерация (RouterAI, существующий интерфейс)
# ---------------------------------------------------------------------------

_TONE_RULES = (
    "Тон: экспертный, живой, деловой — как новостная рассылка для предпринимателей "
    "и внедренцев (референс reymer.ai/news): заголовок по формуле «действующее лицо + результат», "
    "конкретика и цифры вместо общих слов, без канцелярита и без восклицательного популизма. "
    "Зарубежные новости пересказывай по-русски, имена компаний оставляй в оригинале. "
    "НЕ выдумывай факты, которых нет во входных данных; цифры переноси точно. "
    "Никакого Markdown (#, **, `), никаких ссылок внутри текста — только чистый русский текст."
)


def _candidate_prompt_rows(selected: list[ScoredCandidate]) -> list[dict]:
    rows = []
    for s in selected:
        it = s.item
        rows.append(
            {
                "id": it.uid,
                "title": it.title,
                "summary": it.summary,
                "source": it.source_title,
                "url": it.url,
                "lang": it.lang,
                "origin": it.origin,
                "topics": s.topic_labels,
                "high_significance": s.high_significance,
            }
        )
    return rows


async def _chat(messages: list[dict], *, model: str, purpose: str, reason: str) -> str:
    text, usage, _raw = await chat_completion(
        base_url=config.ROUTERAI_BASE_URL,
        api_key=config.ROUTERAI_API_KEY,
        model=model,
        messages=messages,
        timeout_s=config.ROUTERAI_TIMEOUT_READ_CONTENT_S,
    )
    json_log(
        {
            "type": "routerai_usage",
            "purpose": purpose,
            "model": usage.model,
            "tokens_in": usage.input_tokens,
            "tokens_out": usage.output_tokens,
            "cost_usd": usage.cost_usd,
            "routing_reason": reason,
        }
    )
    return str(text)


def _pick_model(candidates: list[ScoredCandidate]) -> str:
    has_legal = any("legal" in s.topics for s in candidates)
    choice = content_choice(has_legal=has_legal, main=config.CONTENT_MODEL_MAIN, heavy=config.CONTENT_MODEL_HEAVY)
    return choice.model


async def generate_blocks(
    *,
    kind: str,
    candidates: list[ScoredCandidate],
    min_blocks: int,
    max_blocks: int,
) -> list[DigestBlock]:
    """Генерация блоков выпуска (заголовок/суть/что для бизнеса) одним вызовом LLM."""
    model = _pick_model(candidates)
    system = (
        "Ты редактор делового дайджеста «АЛТ» (rusalts.ru). Аудитория — заказчики "
        "автоматизации бизнес-процессов с ИИ: собственники, ИТ-директора, юристы компаний. "
        + _TONE_RULES
    )
    if kind == "max_weekly":
        user = (
            "Собери еженедельную сводку новостей мессенджера МАКС. "
            "Это ВСЕ изменения платформы за неделю одним выпуском: обнови читателя, "
            "что появилось, и что из этого можно применить в ботах и интеграциях.\n"
            f"Верни строго ОДИН JSON-объект: {{\"blocks\": [{{\"id\": string, \"headline\": string, "
            "\"body\": string (2–4 предложения сути), \"business\": string (1–2 предложения: что это значит для бизнеса)}}]}} "
            f"— от {min_blocks} до {max_blocks} блоков, по одному на каждую новость из входных, в том же порядке и с теми же id. "
            "Без ```json``` и текста вокруг.\n\n"
            f"Входные данные:\n{json.dumps(_candidate_prompt_rows(candidates), ensure_ascii=False)}"
        )
    elif kind == "standalone":
        user = (
            "Это ВАЖНАЯ отдельная новость (высокая значимость: влияет на бизнес-процессы, "
            "законодательство или крупный релиз ИИ) — выпуск из одного блока. "
            "Заголовок по формуле «действующее лицо + результат», суть — 3–5 предложений "
            "с конкретикой и цифрами, вывод для бизнеса обязателен.\n"
            f"Верни строго ОДИН JSON-объект: {{\"blocks\": [{{\"id\": string, \"headline\": string, "
            "\"body\": string, \"business\": string}}]}} — ровно {max_blocks} блок(а), с тем же id, что у входной новости. "
            "Без ```json``` и текста вокруг.\n\n"
            f"Входные данные:\n{json.dumps(_candidate_prompt_rows(candidates), ensure_ascii=False)}"
        )
    else:
        user = (
            "Собери ежедневный дайджест новостей для бизнеса. "
            "Каждая новость — самостоятельный блок: цепляющий заголовок (формула «действующее лицо + результат», "
            "цифра или выгода прямо в заголовке), 2–4 предложения сути своими словами (не копируй заголовок источника), "
            "и обязательный практический вывод для бизнеса — что это меняет для компаний прямо сейчас.\n"
            f"Верни строго ОДИН JSON-объект: {{\"blocks\": [{{\"id\": string, \"headline\": string, "
            "\"body\": string, \"business\": string}}]}} "
            f"— по одному блоку на каждую новость из входных, в том же порядке и с теми же id. "
            "Без ```json``` и текста вокруг.\n\n"
            f"Входные данные:\n{json.dumps(_candidate_prompt_rows(candidates), ensure_ascii=False)}"
        )
    text = await _chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        model=model,
        purpose=f"news_digest_{kind}_blocks",
        reason="news_digest_blocks",
    )
    obj = _extract_json_object(text)
    blocks = extract_blocks(obj, candidates, max_blocks=max_blocks)
    if len(blocks) < min_blocks:
        raise ValueError(f"llm returned {len(blocks)} blocks, min {min_blocks}")
    return blocks


async def generate_lead(*, kind: str, header: str, blocks: list[DigestBlock]) -> str:
    """Лид-анонс 180–200 знаков — ОТДЕЛЬНОЙ генерацией (ТЗ 2.6), с проверкой длины.

    Это первые ~200 знаков на главной странице — самый продаваемый фрагмент:
    заход с цифрой, интригой или прямой пользой для бизнеса. До 3 попыток,
    затем детерминированный резерв (fit_lead).
    """
    model = config.CONTENT_MODEL_MAIN or config.ROUTERAI_CHAT_MODEL
    bullets = [
        {"headline": b.headline, "body": b.body, "business": b.business} for b in blocks
    ]
    system = (
        "Ты редактор деловых новостей «АЛТ». Пишешь лид-анонс выпуска — один абзац, "
        "который показывается на главной странице сайта первым. " + _TONE_RULES
    )
    user = (
        f"Выпуск ({kind}): {header}. Новости:\n{json.dumps(bullets, ensure_ascii=False)}\n\n"
        f"Напиши лид-анонс этого выпуска: {LEAD_MIN_CHARS}–{LEAD_TARGET_CHARS} знаков С УЧЁТОМ пробелов "
        f"(строго не меньше {LEAD_MIN_CHARS} и не больше {LEAD_MAX_CHARS}). "
        "Заход — цифра, интрига или прямая польза для бизнеса; без слов «Анонс», «Обзор», «Дайджест» в начале; "
        "без перечисления всех новостей; финал должен заставить открыть выпуск. "
        "Не повторяй дословно заголовки блоков.\n"
        "Верни строго ОДИН JSON-объект: {\"lead\": string}. Без ```json``` и текста вокруг."
    )
    last = ""
    for attempt in range(1, 4):
        text = await _chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            model=model,
            purpose=f"news_digest_{kind}_lead",
            reason=f"news_digest_lead_attempt_{attempt}",
        )
        try:
            lead = _WS.sub(" ", str(_extract_json_object(text).get("lead") or "")).strip()
        except Exception:  # noqa: BLE001
            lead = ""
        last = lead or last
        if validate_lead(lead):
            return lead
        json_log({"type": "news_digest_lead_length_retry", "attempt": attempt, "length": len(lead)})
    # Резерв: собираем лид из лучшего блока и подгоняем длину детерминированно.
    top = blocks[0]
    fallback_src = f"{top.headline}. {top.business or top.body}"
    return fit_lead(fallback_src)


# ---------------------------------------------------------------------------
# Дедуп против опубликованного
# ---------------------------------------------------------------------------


def _load_published_index(*, limit: int = 60) -> list[dict]:
    path = config.CONTENT_PUBLISHED_SITE_INDEX_PATH
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [x for x in data[:limit] if isinstance(x, dict)] if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        return []


def _latest_change_items(*, now: datetime) -> list[dict]:
    """Diff-кандидаты из последнего change-пакета (TAdviser, РБК Тренды и др.)."""
    registry_ids = {s.id for s in load_news_sources()}
    for back in range(0, 3):
        day = (now - timedelta(days=back)).date()
        pkg = config.CHANGES_DIR / day.isoformat() / "change_package.json"
        if not pkg.is_file():
            continue
        try:
            raw = json.loads(pkg.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        items = [x for x in (raw.get("items") or []) if isinstance(x, dict) and str(x.get("source_id") or "") in registry_ids]
        if items:
            return items
        return []
    return []


# ---------------------------------------------------------------------------
# Сохранение выпуска в очередь
# ---------------------------------------------------------------------------


def _save_release_item(
    *,
    kind: str,
    title: str,
    lead: str,
    blocks: list[DigestBlock],
    candidates: list[ScoredCandidate],
    model: str,
    auto_publish: bool,
) -> str:
    urls = [b.url for b in blocks if b.url]
    site_text = assemble_site_text(lead, blocks, urls)
    site_text = strip_markdown_public(site_text).strip()
    vk_text = assemble_vk_text(lead, blocks)
    topics = sorted({t for c in candidates for t in c.topic_labels})
    note = (
        f"news_{kind} | model={model} | news={len(blocks)} | topics={', '.join(topics) or '—'} | "
        f"lead_chars={len(lead)}"
    )
    pub_id = next_publication_id()
    item = ContentItem(
        publication_id=pub_id,
        created_at_utc=_utc_now_iso(),
        status="pending",
        title=title[:200],
        site_text=site_text,
        vk_text=vk_text,
        internal_note=note,
        sources=urls[:20],
    )
    save_item(item)
    json_log(
        {
            "type": "news_digest_item_saved",
            "kind": kind,
            "publication_id": pub_id,
            "news": len(blocks),
            "lead_chars": len(lead),
            "auto_publish": auto_publish,
        }
    )
    return pub_id


# ---------------------------------------------------------------------------
# Главный конвейер
# ---------------------------------------------------------------------------


@dataclass
class DigestRunResult:
    ok: bool = False
    reason: str = ""
    digest_publication_id: str = ""
    standalone_publication_ids: list[str] = field(default_factory=list)
    max_weekly_publication_id: str = ""
    max_pool_size: int = 0
    candidates_fetched: int = 0
    duplicates_skipped: int = 0
    selected_for_digest: int = 0


async def run_news_digest_pipeline(payload: dict | None = None) -> DigestRunResult:
    payload = payload or {}
    force = bool(payload.get("force"))
    auto_publish = bool(payload.get("auto_publish"))  # по умолчанию НЕ публикуем (очередь /publapprov/)
    now = datetime.now(timezone.utc)
    state = load_digest_state()
    res = DigestRunResult()

    # 1) Анти-дубль запусков: не чаще раза в DIGEST_COOLDOWN_HOURS (если не force).
    last = state.last_digest_utc
    if not force and last:
        try:
            last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            if (now - last_dt) < timedelta(hours=DIGEST_COOLDOWN_HOURS):
                res.reason = "cooldown"
                return res
        except Exception:  # noqa: BLE001
            pass

    # 2) Кандидаты: RSS-ленты + diff-кандидаты последнего change-пакета.
    raw_items = await fetch_all_feeds()
    change_raw = _latest_change_items(now=now)
    if change_raw:
        raw_items.extend(change_items_to_news(change_raw, now_utc=_utc_now_iso()))
    res.candidates_fetched = len(raw_items)

    # 3) Дедуп фактов против опубликованного + уже отдайджестенного.
    topic_cfg = load_topic_config()
    sources = {s.id: s for s in load_news_sources()}
    dedup = FactDedup(published=_load_published_index(), state=state, now=now)
    scored: list[ScoredCandidate] = []
    for it in raw_items:
        verdict = dedup.check(title=it.title, summary=it.summary, url=it.url)
        if verdict.duplicate:
            res.duplicates_skipped += 1
            continue
        scored.append(score_candidate(it, topic_cfg, source_priority=sources.get(it.source_id).priority if sources.get(it.source_id) else "medium"))

    # 4) Правило МАКС: копим в пул, в ежедневный дайджест не берём (ТЗ 2.3).
    max_items = [s.item for s in scored if s.is_max]
    if max_items:
        add_to_max_pool(state, max_items)
    res.max_pool_size = len(state.max_pool)
    non_max = [s for s in scored if not s.is_max]

    # 5) Ранжирование и отбор (ТЗ 2.4): значимые кандидаты → отдельные выпуски (до 2),
    #    остальные → ежедневный дайджест. Если без значимых дайджест не набирается —
    #    значимые возвращаются в дайджест: ежедневный формат — базовый.
    ranked = rank_candidates(non_max)
    standalone = [s for s in ranked if s.high_significance][:STANDALONE_MAX_PER_DAY]
    standalone_uids = {s.uid for s in standalone}
    digest_pool = [s for s in ranked if s.uid not in standalone_uids]
    while standalone and len(digest_pool) < DIGEST_MIN_NEWS:
        moved = standalone.pop()
        digest_pool.insert(0, moved)
    selected = select_for_digest(rank_candidates(digest_pool), min_news=DIGEST_MIN_NEWS, max_news=DIGEST_MAX_NEWS)
    res.selected_for_digest = len(selected)

    created_any = False

    # 6) Ежедневный дайджест «Обзор: N новостей». Ежедневный формат — базовый:
    # выпускаем и на 1–2 новостях (медленный день), минимум не блокирует выход.
    if selected:
        try:
            blocks = await generate_blocks(kind="daily", candidates=selected, min_blocks=1, max_blocks=DIGEST_MAX_NEWS)
            header = f"ежедневный обзор, {len(blocks)} новостей"
            lead = await generate_lead(kind="daily", header=header, blocks=blocks)
            title = f"Обзор: {len(blocks)} новостей для бизнеса"
            model = _pick_model(selected)
            pub_id = _save_release_item(
                kind="digest",
                title=title,
                lead=lead,
                blocks=blocks,
                candidates=[b.candidate for b in blocks if b.candidate],
                model=model,
                auto_publish=auto_publish,
            )
            res.digest_publication_id = pub_id
            created_any = True
            for b in blocks:
                if b.candidate:
                    dedup.remember(title=b.headline, summary=b.business or b.body, url=b.url)
        except Exception as e:  # noqa: BLE001
            json_log({"type": "news_digest_daily_failed", "error": str(e)})

    # 7) Отдельные выпуски — только высокая значимость (ТЗ 2.4).
    for cand in standalone:
        try:
            blocks = await generate_blocks(kind="standalone", candidates=[cand], min_blocks=1, max_blocks=1)
            lead = await generate_lead(kind="standalone", header=cand.item.title[:120], blocks=blocks)
            model = _pick_model([cand])
            pub_id = _save_release_item(
                kind="standalone",
                title=blocks[0].headline[:200],
                lead=lead,
                blocks=blocks,
                candidates=[cand],
                model=model,
                auto_publish=auto_publish,
            )
            res.standalone_publication_ids.append(pub_id)
            created_any = True
            b = blocks[0]
            dedup.remember(title=b.headline, summary=b.business or b.body, url=b.url)
        except Exception as e:  # noqa: BLE001
            json_log({"type": "news_digest_standalone_failed", "uid": cand.uid, "error": str(e)})

    # 8) Еженедельный релиз-сводка МАКС (пятница, ТЗ 2.3).
    if max_weekly_due(state, now=now):
        try:
            pool_items = [pool_record_to_news_item(r) for r in state.max_pool]
            pool_scored = [score_candidate(it, topic_cfg, source_priority=sources.get(it.source_id).priority if sources.get(it.source_id) else "low") for it in pool_items]
            pool_ranked = rank_candidates(pool_scored)
            pool_sel = pool_ranked[:8]
            if len(pool_sel) >= 2:
                blocks = await generate_blocks(kind="max_weekly", candidates=pool_sel, min_blocks=2, max_blocks=8)
                header = "новости МАКС за неделю"
                lead = await generate_lead(kind="max_weekly", header=header, blocks=blocks)
                title = f"МАКС: сводка недели — {len(blocks)} обновлени{'я' if len(blocks) in (2,3,4) else 'й'}"
                model = _pick_model(pool_sel)
                pub_id = _save_release_item(
                    kind="max_weekly",
                    title=title,
                    lead=lead,
                    blocks=blocks,
                    candidates=[b.candidate for b in blocks if b.candidate],
                    model=model,
                    auto_publish=auto_publish,
                )
                res.max_weekly_publication_id = pub_id
                created_any = True
                for b in blocks:
                    dedup.remember(title=b.headline, summary=b.business or b.body, url=b.url)
                state.max_pool = []
                state.last_max_release_utc = _utc_now_iso()
        except Exception as e:  # noqa: BLE001
            json_log({"type": "news_digest_max_weekly_failed", "error": str(e)})

    if created_any:
        state.last_digest_utc = _utc_now_iso()
        res.ok = True
        res.reason = "created"
    else:
        res.reason = res.reason or "nothing_to_publish"
    save_digest_state(state)
    json_log({"type": "news_digest_run_done", **{k: v for k, v in res.__dict__.items()}})
    return res
