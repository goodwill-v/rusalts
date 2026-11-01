from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from app import config
from app.content_excerpt import title_fallback_from_site_text
from app.content_store import (
    ContentItem,
    item_exists,
    load_item,
    next_publication_id,
    save_item,
    set_status,
    update_item,
)
from app.content_publish_flow import approve_publication_by_id
from app.markdown_plain import clean_public_text_fragment, strip_markdown_public
from app.observability import json_log
from app.publishers.site import publish_to_site
from app.queue_bus import CONSUMER_NAME, GROUP_CONTENT, STREAM_CONTENT_JOBS, consume_one, ensure_groups, get_redis
from app.model_routing import content_choice
from app.routerai import RouterAIError, chat_completion


def _is_legal(items: list[dict]) -> bool:
    for it in items or []:
        c = str(it.get("classification") or "")
        if c.startswith("legal."):
            return True
    return False


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _extract_article_excerpt(article_path: str, *, max_chars: int = 900) -> str:
    """
    ChangeItem сам по себе содержит только summary + ссылки.
    Для осмысленного черновика читаем соответствующую KB-статью, которую создал парсер,
    и берём короткий фрагмент «извлечённого текста».
    """
    p = Path(str(article_path or "").strip())
    if not p.is_file():
        return ""
    try:
        raw = p.read_text(encoding="utf-8")
    except Exception:
        return ""
    # Парсер пишет в body блок: "Извлечённый текст (автоматически):"
    marker = "Извлечённый текст (автоматически):"
    i = raw.find(marker)
    if i != -1:
        raw = raw[i + len(marker) :]
    raw = " ".join((raw or "").split()).strip()
    if not raw:
        return ""
    return (raw[: max_chars - 1] + "…") if len(raw) > max_chars else raw


def _augment_items(items: list[dict], *, max_items: int = 40) -> list[dict]:
    out: list[dict] = []
    for it in (items or [])[:max_items]:
        if not isinstance(it, dict):
            continue
        excerpt = _extract_article_excerpt(str(it.get("article_path") or ""))
        it2 = dict(it)
        if excerpt:
            it2["excerpt"] = excerpt
        out.append(it2)
    return out


_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)


def _domain_label(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        host = ""
    if not host:
        return "источник"
    if host.startswith("www."):
        host = host[4:]
    return host


def _extract_json_object(text: str) -> dict:
    """
    RouterAI модели иногда возвращают JSON в fenced-блоке ```json ...``` или
    добавляют поясняющий текст вокруг. Мы обязаны извлечь JSON и НЕ публиковать
    технические части.
    """
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty llm output")

    # 1) Direct JSON
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    # 2) Fenced JSON blocks
    for m in _FENCED_JSON_RE.finditer(raw):
        chunk = (m.group(1) or "").strip()
        if not chunk:
            continue
        try:
            obj = json.loads(chunk)
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue

    # 3) Heuristic: first '{' ... last '}' window
    i = raw.find("{")
    j = raw.rfind("}")
    if 0 <= i < j:
        chunk = raw[i : j + 1].strip()
        try:
            obj = json.loads(chunk)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass

    raise ValueError("could not extract JSON object")


def _clean_public_text(s: str) -> str:
    """Совместимость: см. app.markdown_plain.clean_public_text_fragment."""
    return clean_public_text_fragment(s)


def _strip_residual_markdown(s: str) -> str:
    """Совместимость: см. app.markdown_plain.strip_markdown_public."""
    return strip_markdown_public(s)


def _load_recent_publications(*, limit: int = 12) -> list[dict]:
    """
    Берём последние опубликованные записи (для анти-дубля и контекста редакции).
    index.json ведётся в app.publishers.site.publish_to_site.
    """
    try:
        path = config.CONTENT_PUBLISHED_SITE_INDEX_PATH
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return []
        out: list[dict] = []
        for row in data[: max(1, int(limit))]:
            if not isinstance(row, dict):
                continue
            out.append(
                {
                    "publication_id": str(row.get("publication_id") or "").strip(),
                    "published_at_utc": str(row.get("published_at_utc") or "").strip(),
                    "title": str(row.get("title") or "").strip(),
                    "excerpt": str(row.get("excerpt") or "").strip(),
                    "url": str(row.get("url") or "").strip(),
                    "sources": row.get("sources") or [],
                }
            )
        return out
    except Exception:
        return []


def _inject_inline_sources(site_text: str, sources: list[str]) -> str:
    """
    Требование: если источников несколько — вставлять ссылки по ходу текста.
    Практически: если некоторых URL нет в тексте, добавляем их сразу после первого абзаца
    как строки «домен — https://...». Если источник один — добавляем в конец.
    """
    text = (site_text or "").strip()
    if not text:
        return ""
    urls = [str(u).strip() for u in (sources or []) if str(u).strip()]
    urls = list(dict.fromkeys(urls))  # stable unique
    if not urls:
        return text

    missing = [u for u in urls if u not in text]
    if not missing:
        return text

    def _fmt(u: str) -> str:
        return f"{_domain_label(u)} — {u}"

    if len(urls) == 1:
        # Один источник — в конец
        return (text + "\n\n" + _fmt(urls[0])).strip()

    # Несколько источников — сразу после первого абзаца
    parts = text.split("\n\n", 1)
    first = parts[0].strip()
    rest = parts[1].strip() if len(parts) > 1 else ""
    links_block = "\n".join(_fmt(u) for u in missing[:10]).strip()
    if rest:
        return (first + "\n\n" + links_block + "\n\n" + rest).strip()
    return (first + "\n\n" + links_block).strip()


async def refine_corporate_publication(it: ContentItem) -> tuple[str, str, str, str, str]:
    """
    Творческая обработка черновика с портала: два канала, без Markdown и служебных инструкций в публичных полях.
    Возвращает: title, site_plain, vk_plain, internal_note, model.
    """
    raw_site = (it.site_text or "").strip()
    raw_vk = (it.vk_text or "").strip() or raw_site
    sources = [str(u).strip() for u in (it.sources or []) if str(u).strip()]
    sources_for_prompt = [{"label": _domain_label(u), "url": u} for u in sources[:20]]

    choice = content_choice(has_legal=False, main=config.CONTENT_MODEL_MAIN, heavy=config.CONTENT_MODEL_HEAVY)
    model = choice.model

    payload = {
        "title_in": (it.title or "").strip(),
        "site_draft": raw_site,
        "vk_draft": raw_vk,
        "sources": sources_for_prompt,
        "rules": {
            "no_markdown_in_public": True,
            "no_prefix_announce": True,
            "no_technical_meta_in_public": True,
            "tone_site": "официально-деловой, экспертный, нейтральный, как новость на корпоративном сайте",
            "tone_vk": "живой, вовлекающий, короткие абзацы, умеренные эмодзи, призыв и хэштеги в конце",
        },
        "output": {
            "title": "string (служебное, см. ниже — дублирует начало site_text_plain)",
            "site_text_plain": "string (plain text only, no # ** ` []() markdown)",
            "vk_text_plain": "string (plain text only)",
            "internal_note": "string (PRIVATE, факты для редактора, без фраз про «тон выдержан»)",
        },
    }

    messages = [
        {
            "role": "system",
            "content": (
                "Ты редактор корпоративных коммуникаций АЛТ. "
                "Превращаешь сырые черновики в увлекательные публикации для двух аудиторий: "
                "Сайт (профессиональный, экспертный, живой русский язык без канцелярита) и "
                "ВК (вовлекающий, коротко, с эмодзи и призывом к действию). "
                "Публичные поля: обычный русский текст, никаких символов Markdown (#, **, `, []()), "
                "никаких JSON-блоков и служебных инструкций. "
                "Первый абзац — крючок, который заставляет читателя продолжить чтение. "
                "Не выдумывай факты — переформулируй то, что дано."
            ),
        },
        {
            "role": "user",
            "content": (
                "Сгенерируй результат строго как ОДИН JSON-объект, без ```json``` и без текста вокруг.\n"
                "Ключевое: отдельного заголовка на сайте нет — первый абзац `site_text_plain` одновременно задаёт тон и суть; "
                "он же попадёт в ленту как превью (до ~200 знаков). Сформулируй первый абзац максимально цепко и интересно, "
                "без префиксов «Анонс:», без дублирования «шапки».\n"
                "Требования:\n"
                "- `site_text_plain`: статья для сайта, живой русский язык, без эмодзи. "
                "Первый абзац — крючок (~100–200 знаков), дальше развёрнутый текст. "
                "После первого абзаца добавь блок «Почему это важно» (2-3 предложения, объясняет ценность).\n"
                "- Ссылки на источники: не используй подписи «Источник:». "
                "Один URL — в конце «<домен> — https://...». Несколько — по ходу или блоком строк.\n"
                "- `vk_text_plain`: для ВКонтакте; первый абзац — крючок (200 знаков); "
                "далее короткие абзацы по 2–3 строки, умеренные эмодзи (🔹✅⚠️🔥🚀), "
                "в конце призыв к действию и хэштеги #АЛТ #автоматизация или #ЗаконыИТ #AI.\n"
                "- `title`: совпадает с началом первого абзаца `site_text_plain` (до 100 знаков).\n"
                "- `internal_note`: только факты для редакции.\n\n"
                f"Входные данные:\n{json.dumps(payload, ensure_ascii=False)}"
            ),
        },
    ]

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
            "request_id": uuid.uuid4().hex,
            "model": usage.model,
            "tokens_in": usage.input_tokens,
            "tokens_out": usage.output_tokens,
            "cost_usd": usage.cost_usd,
            "purpose": "content_refine_corporate",
            "routing_reason": choice.reason,
        }
    )

    try:
        obj = _extract_json_object(str(text))
    except Exception:
        if config.CONTENT_MODEL_HEAVY and model != config.CONTENT_MODEL_HEAVY:
            choice2 = content_choice(has_legal=True, main=config.CONTENT_MODEL_MAIN, heavy=config.CONTENT_MODEL_HEAVY)
            text2, usage2, _raw2 = await chat_completion(
                base_url=config.ROUTERAI_BASE_URL,
                api_key=config.ROUTERAI_API_KEY,
                model=choice2.model,
                messages=messages,
                timeout_s=config.ROUTERAI_TIMEOUT_READ_CONTENT_RETRY_S,
            )
            json_log(
                {
                    "type": "routerai_usage",
                    "request_id": uuid.uuid4().hex,
                    "model": usage2.model,
                    "tokens_in": usage2.input_tokens,
                    "tokens_out": usage2.output_tokens,
                    "cost_usd": usage2.cost_usd,
                    "purpose": "content_refine_corporate_retry",
                    "routing_reason": "json_extract_failed",
                }
            )
            obj = _extract_json_object(str(text2))
            model = choice2.model
        else:
            raise

    if not isinstance(obj, dict):
        raise ValueError("LLM output is not a JSON object")

    site_plain = _strip_residual_markdown(str(obj.get("site_text_plain") or "")).strip()
    vk_plain = _strip_residual_markdown(str(obj.get("vk_text_plain") or "")).strip()
    internal_note = str(obj.get("internal_note") or "").strip()
    internal_note = _clean_public_text(internal_note)

    if not site_plain:
        raise ValueError("LLM returned empty public text fields")
    if len(vk_plain) < 10:
        vk_plain = site_plain
    if site_plain.lstrip().startswith("{") or vk_plain.lstrip().startswith("{"):
        raise ValueError("LLM returned JSON-like public text")

    title = title_fallback_from_site_text(site_plain)
    return title, site_plain, vk_plain, internal_note, model


def _fallback_publication(*, change_package_path: str, items: list[dict], error: str) -> tuple[str, str, str, list[str], str]:
    """
    Если RouterAI недоступен, всё равно создаём черновик в очереди одобрения.
    Это важно для автоматической работы: система продолжает “жить”, а редактор
    может одобрить/исправить вручную.
    """
    sources = sorted({str(it.get("source_url") or "").strip() for it in items if it.get("source_url")})
    sources = [s for s in sources if s]
    # Заголовок (без фантазии, но понятный)
    title = f"Черновик новости: найдено изменений — {len(items or [])}"

    def _one_line(it: dict) -> str:
        src = str(it.get("source_title") or it.get("source_id") or "").strip()
        url = str(it.get("source_url") or "").strip()
        summary = str(it.get("summary") or it.get("title") or it.get("change") or "").strip()
        cls = str(it.get("classification") or "").strip()
        excerpt = str(it.get("excerpt") or "").strip()
        parts = [p for p in [summary, cls] if p]
        core = " — ".join(parts) if parts else (src or "изменение")
        if excerpt:
            core = f"{core}\n  - Фрагмент: {excerpt}"
        if url:
            return f"- {core}\n  - {url}"
        return f"- {core}"

    body_lines = ["### Общий формат публикаций", "", f"Источник пакета: `{change_package_path}`", ""]
    if error:
        body_lines += ["**Примечание:** автогенерация не удалась (RouterAI). Черновик создан автоматически.", f"Ошибка: `{error}`", ""]
    if items:
        body_lines += ["#### Найденные изменения", ""]
        body_lines += [_one_line(it) for it in items[:50]]
        body_lines += [""]
    if sources:
        body_lines += ["#### Официальные источники", ""]
        body_lines += [f"- {s}" for s in sources[:50]]
        body_lines += [""]

    site_text = "\n".join(body_lines).strip() + "\n"
    vk_lines = [title, ""]
    for it in (items or [])[:8]:
        s = str(it.get("summary") or "").strip()
        src = str(it.get("source_title") or it.get("source_id") or "").strip()
        u = str(it.get("source_url") or "").strip()
        if not (s or src):
            continue
        line = f"- {s or 'Изменение'}"
        if src:
            line += f" ({src})"
        vk_lines.append(line)
        if u:
            vk_lines.append(u)
    vk_text = "\n".join(vk_lines).strip()
    if sources:
        vk_text += "\n\nИсточники:\n" + "\n".join(sources[:5])
    used_model = "fallback(no_routerai)"
    return title, site_text, vk_text, sources, used_model


async def _generate_texts(*, change_package_path: str, items: list[dict]) -> tuple[str, str, str, list[str], str]:
    # Enrich items with short excerpts from KB articles, so RouterAI can produce a meaningful summary.
    items_aug = _augment_items(items)
    sources = sorted({str(it.get("source_url") or "").strip() for it in items_aug if it.get("source_url")})
    sources = [s for s in sources if s]
    recent = _load_recent_publications(limit=12)

    has_legal = _is_legal(items_aug)
    choice = content_choice(has_legal=has_legal, main=config.CONTENT_MODEL_MAIN, heavy=config.CONTENT_MODEL_HEAVY)
    model = choice.model

    sources_for_prompt = [{"label": _domain_label(u), "url": u} for u in sources[:20]]

    # Определяем доминирующий тип контента из классификации ChangeItem
    content_types = [str(it.get("classification") or "") for it in items_aug if it.get("classification")]
    top_type = max(set(content_types), key=content_types.count) if content_types else "platform.update"

    prompt = {
        "change_package_path": change_package_path,
        "content_type": top_type,
        "items": items_aug[:40],
        "sources": sources_for_prompt,
        "recent_publications": recent[:12],
        "rules": {
            "no_legal_advice": True,
            "must_cite_sources": True,
            "tone_site": "официально-деловой, экспертный, нейтральный",
            "tone_vk": "динамичный, вовлекающий, экспертный но дружеский",
            "no_technical_notes_in_public_text": True,
            "no_duplicate_story_against_recent": True,
        },
        "output": {
            "title": "string (служебное: начало первого абзаца site_text_md, до 100 знаков)",
            "site_text_md": "string (обычный текст для сайта: без Markdown # ** [](); абзацы через пустую строку)",
            "vk_text": "string (plain text)",
            "internal_note": "string (PRIVATE, not for publication)",
        },
    }

    messages = [
        {
            "role": "system",
            "content": (
                "Ты контент-редактор АЛТ (альтернативные легальные технологии). "
                "Твоя задача — превратить технические обновления в увлекательные публикации, которые: \n"
                "1) Привлекают внимание посетителя в первой же строке\n"
                "2) Объясняют «почему это важно для меня»\n"
                "3) Ориентируют на услуги АЛТ (автоматизация, автономизация, юридический комплаенс, новинки ИИ)\n"
                "Нельзя публиковать JSON, служебные инструкции или внутренние заметки. "
                "Юридически чувствительные изменения — формулируй осторожно, без юр. советов, с ссылками на первоисточники."
            ),
        },
        {
            "role": "user",
            "content": (
                "Сгенерируй публикацию строго в виде ОДНОГО JSON-объекта, без ```json``` и без любого текста вокруг.\n"
                "\n"
                "### Определи тип публикации по входным данным:\n"
                "- **release** — релиз новой версии/функции платформы: пиши про возможности, кому это нужно, как начать\n"
                "- **legal** — изменение закона/регуляторики: что изменилось, как влияет на бизнес, что делать\n"
                "- **case-study** — кейс внедрения/пример: проблема → решение → результат → как повторить\n"
                "- **tool** — новый инструмент/сервис: что делает, зачем нужен, ссылка\n"
                "- **analytics** — тренд/аналитика: ключевые цифры, выводы, рекомендация\n"
                "\n"
                "Для каждого типа — свой tone-of-voice:\n"
                "- **release**: первый абзац — интрига «что появилось», затем «кому это полезно», в конце «где взять»\n"
                "- **legal**: первый абзац — «что изменилось в законе?», затем «как это касается вашего бизнеса», в конце «что делать» с отсылкой к услугам АЛТ\n"
                "- **case-study**: заголовок-интрига, затем проблема → решение → результат (цифры), в конце «как внедрить у себя»\n"
                "- **tool**: «что за инструмент», «какую проблему решает», «стоимость/доступность», ссылка\n"
                "- **analytics**: ключевой вывод в первой строке, затем цифры/факты, в конце прогноз\n"
                "\n"
                "### Структура публикации:\n"
                "- `title`: совпадает с началом первого абзаца `site_text_md` (до 100 знаков), только служебное поле.\n"
                "- САЙТ (`site_text_md`): экспертный, живой русский язык, без канцелярита. Без Markdown.\n"
                "  - Первый абзац — **крючок**: 1 абзац, ~100–200 знаков, заставляет читать дальше. "
                "  - Блок «Почему это важно» (2-3 предложения после первого абзаца) — объясняем ценность для бизнеса/блогера/НКО\n"
                "  - Далее — факты, контекст, развитие\n"
                "  - Ссылки: без подписи «Источник:». Один URL — в конце как «<домен> — https://...». Несколько — по ходу текста или блоком строк.\n"
                "- ВК (`vk_text`): первый абзац — крючок (200 знаков), далее абзацы по 2–3 строки, эмодзи (🔹✅⚠️🚀🔥⚖️), "
                "в конце призыв к действию и #АЛТ #автоматизация #цифровой_суверенитет или #ЗаконыИТ #AI\n"
                "- Анти-дубль: если новая публикация похожа на `recent_publications` — фокус только на новом, отдели от уже известного.\n"
                "- `internal_note`: факты для редактора, без комментариев про стиль.\n"
                "\n"
                f"Входные данные:\n{json.dumps(prompt, ensure_ascii=False)}"
            ),
        },
    ]

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
            "request_id": uuid.uuid4().hex,
            "model": usage.model,
            "tokens_in": usage.input_tokens,
            "tokens_out": usage.output_tokens,
            "cost_usd": usage.cost_usd,
            "purpose": "content_generate_from_change_package",
            "routing_reason": choice.reason,
        }
    )

    title = "Обновления АЛТ‑эксперт"
    site_text = ""
    vk_text = ""
    internal_note = f"auto: generated from {change_package_path}"

    try:
        obj = _extract_json_object(str(text))
    except Exception:
        # Retry once with heavy if configured and we weren't on heavy yet.
        if config.CONTENT_MODEL_HEAVY and model != config.CONTENT_MODEL_HEAVY:
            choice2 = content_choice(has_legal=True, main=config.CONTENT_MODEL_MAIN, heavy=config.CONTENT_MODEL_HEAVY)
            text2, usage2, _raw2 = await chat_completion(
                base_url=config.ROUTERAI_BASE_URL,
                api_key=config.ROUTERAI_API_KEY,
                model=choice2.model,
                messages=messages,
                timeout_s=config.ROUTERAI_TIMEOUT_READ_CONTENT_RETRY_S,
            )
            json_log(
                {
                    "type": "routerai_usage",
                    "request_id": uuid.uuid4().hex,
                    "model": usage2.model,
                    "tokens_in": usage2.input_tokens,
                    "tokens_out": usage2.output_tokens,
                    "cost_usd": usage2.cost_usd,
                    "purpose": "content_generate_from_change_package_retry",
                    "routing_reason": "content_retry_json_extract_failed",
                }
            )
            obj = _extract_json_object(str(text2))
            model = choice2.model
        else:
            raise

    if not isinstance(obj, dict):
        raise ValueError("LLM output is not a JSON object")

    site_text = _clean_public_text(str(obj.get("site_text_md") or "")).strip()
    vk_text = _clean_public_text(str(obj.get("vk_text") or "")).strip()
    internal_note = str(obj.get("internal_note") or internal_note).strip()
    site_text = _strip_residual_markdown(site_text)
    vk_text = _strip_residual_markdown(vk_text)
    site_text = _inject_inline_sources(site_text, sources)
    title = title_fallback_from_site_text(site_text)

    # Minimal validation: public texts must not be empty and must not contain JSON braces-only dumps.
    if not site_text or not vk_text:
        raise ValueError("LLM returned empty public text fields")
    if site_text.lstrip().startswith("{") or vk_text.lstrip().startswith("{"):
        raise ValueError("LLM returned JSON-like public text")

    return title, site_text, vk_text, sources, model


_ID5 = re.compile(r"^\d{5}$")


async def refine_corporate_item_by_id(publication_id: str) -> tuple[bool, str]:
    """
    Обработка корпоративного черновика: LLM (или снятие Markdown при отключённой обязательности LLM).
    Возвращает (успех, сообщение об ошибке при неуспехе).
    """
    if not _ID5.match(publication_id) or not item_exists(publication_id):
        return False, "missing_or_bad_id"
    it0 = load_item(publication_id)
    if it0.status not in ("pending", "needs_edit"):
        return False, "not_in_queue"

    max_attempts = 4 if config.CONTENT_LLM_REQUIRED else 1
    err_s = ""
    for attempt in range(1, max_attempts + 1):
        it = load_item(publication_id)
        try:
            title, site_plain, vk_plain, note_llm, used_model = await refine_corporate_publication(it)
            merged_note = (it.internal_note or "").strip()
            if merged_note:
                merged_note += "\n"
            merged_note += f"refined | model={used_model}"
            if note_llm:
                merged_note += "\n" + note_llm
            merged_note = merged_note[:50_000]
            update_item(
                publication_id,
                title=title,
                site_text=site_plain,
                vk_text=vk_plain,
                internal_note=merged_note,
                last_publish_error=None,
            )
            if it.status == "needs_edit":
                set_status(publication_id, status="pending", message_id="corporate_refined")
            json_log({"type": "content_corporate_refined", "publication_id": publication_id, "model": used_model})
            return True, ""
        except RouterAIError as e:
            err_s = str(e) or "RouterAI request failed"
            json_log(
                {
                    "type": "content_corporate_refine_routerai",
                    "publication_id": publication_id,
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "error": err_s,
                }
            )
        except Exception as e:  # noqa: BLE001
            err_s = str(e) or "corporate refine failed"
            json_log(
                {
                    "type": "content_corporate_refine_failed",
                    "publication_id": publication_id,
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "error": err_s,
                }
            )
        if attempt < max_attempts:
            await asyncio.sleep(2.0**attempt)
            continue

    if config.CONTENT_LLM_REQUIRED:
        it = load_item(publication_id)
        site_out = _strip_residual_markdown(it.site_text or "") or (it.site_text or "").strip()
        vk_raw = (it.vk_text or "").strip() or site_out
        vk_out = _strip_residual_markdown(vk_raw) or site_out
        if len(vk_out) < 10:
            vk_out = site_out
        title_u = title_fallback_from_site_text(site_out)
        update_item(
            publication_id,
            title=title_u,
            site_text=site_out,
            vk_text=vk_out,
            last_publish_error=(err_s[:4000] if err_s else "refine failed"),
        )
        set_status(publication_id, status="needs_edit", message_id="corporate_refine_failed")
        json_log({"type": "content_corporate_refine_degraded", "publication_id": publication_id, "error": err_s})
        # Текст уже нормализован (strip + ВК), очередь можно сохранять и править.
        return True, ""

    it = load_item(publication_id)
    site_f = _strip_residual_markdown(it.site_text or "")
    vk_raw = (it.vk_text or "").strip() or (it.site_text or "")
    vk_f = _strip_residual_markdown(vk_raw)
    title_keep = (it.title or "").strip()
    site_out = site_f or (it.site_text or "").strip()
    vk_out = vk_f or site_out
    update_item(publication_id, title=title_keep, site_text=site_out, vk_text=vk_out, last_publish_error=None)
    if it.status == "needs_edit":
        set_status(publication_id, status="pending", message_id="corporate_strip_only")
    json_log({"type": "content_corporate_strip_fallback", "publication_id": publication_id})
    return True, ""


async def handle_content_corporate_draft(*, payload: dict) -> None:
    """Корпоративный черновик: творческая обработка под каналы; опционально сразу публикация."""
    pub_id = str(payload.get("publication_id") or "").strip()
    auto_publish = bool(payload.get("auto_publish"))
    ok, err = await refine_corporate_item_by_id(pub_id)
    if not ok:
        json_log({"type": "content_corporate_draft_fail", "publication_id": pub_id, "error": err})
        return
    json_log({"type": "content_corporate_draft_ok", "publication_id": pub_id, "auto_publish": auto_publish})
    if auto_publish:
        await approve_publication_by_id(request_id=f"corp-auto-{pub_id}", publication_id=pub_id)
        json_log({"type": "content_corporate_auto_published", "publication_id": pub_id})


async def handle_content_from_change_package(*, payload: dict) -> None:
    config.ensure_data_dirs()
    change_package_path = str(payload.get("change_package_path") or "").strip()
    items = payload.get("items") or []
    if not isinstance(items, list):
        items = []

    used_model = ""
    err_s = ""
    # LLM should be mandatory for content generation (channels expansion); we retry on transient RouterAI failures.
    max_attempts = 4 if config.CONTENT_LLM_REQUIRED else 1
    for attempt in range(1, max_attempts + 1):
        try:
            title, site_text, vk_text, sources, used_model = await _generate_texts(
                change_package_path=change_package_path, items=items
            )
            err_s = ""
            break
        except RouterAIError as e:
            err_s = str(e) or "RouterAI request failed"
            json_log(
                {
                    "type": "content_routerai_failed",
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "change_package_path": change_package_path,
                    "error": err_s,
                }
            )
            if attempt < max_attempts:
                await asyncio.sleep(2.0**attempt)
                continue
            # If we still fail and LLM is required: do NOT pretend we generated; create a draft marked needs_edit.
            title, site_text, vk_text, sources, used_model = _fallback_publication(
                change_package_path=change_package_path, items=_augment_items(items, max_items=50), error=err_s
            )
        except Exception as e:  # noqa: BLE001
            err_s = str(e) or "content generation failed"
            title, site_text, vk_text, sources, used_model = _fallback_publication(
                change_package_path=change_package_path, items=_augment_items(items, max_items=50), error=err_s
            )
            break

    pub_id = next_publication_id()
    # Собираем классификации из всех items для внутренней заметки
    all_classifications = sorted({str(it.get("classification") or "") for it in items if it.get("classification")})
    classification_summary = ", ".join(all_classifications[:5]) if all_classifications else "unknown"
    status = "pending"
    if config.CONTENT_LLM_REQUIRED and err_s:
        # Signal in UI that auto-generation failed and needs manual action; do not autoapprove/publish.
        status = "needs_edit"
    item = ContentItem(
        publication_id=pub_id,
        created_at_utc=_now_utc_iso(),
        status=status,
        title=title,
        site_text=site_text,
        vk_text=vk_text,
        internal_note=(f"{payload.get('ts_utc') or ''} | model={used_model} | types:{classification_summary}\n" + (payload.get("internal_note") or "")).strip(),
        sources=sources,
        last_publish_error=(err_s or None),
    )
    save_item(item)

    # For server automation we default to local_autoapprove; can be switched to email later.
    if config.CONTENT_APPROVAL_MODE == "local_autoapprove" and not (config.CONTENT_LLM_REQUIRED and err_s):
        set_status(pub_id, status="approved", message_id="queue_autoapprove")
        item = load_item(pub_id)
        try:
            pub_at, url_path = publish_to_site(item)
            update_item(pub_id, site_published_at_utc=pub_at, site_url=url_path, last_publish_error=None)
        except Exception as e:  # noqa: BLE001
            update_item(pub_id, last_publish_error=str(e))
        json_log({"type": "content_published_from_queue", "publication_id": pub_id, "source": "parser", "change_package_path": change_package_path})
    else:
        json_log({"type": "content_queued_pending_approval", "publication_id": pub_id, "approval_mode": config.CONTENT_APPROVAL_MODE})


async def main() -> None:
    r = await get_redis()
    await ensure_groups(r)

    consumer = f"{CONSUMER_NAME}-content"
    json_log({"type": "worker_started", "worker": "content", "consumer": consumer})

    while True:
        item = await consume_one(r=r, stream=STREAM_CONTENT_JOBS, group=GROUP_CONTENT, consumer=consumer)
        if item is None:
            continue
        msg_id, msg = item
        rid = uuid.uuid4().hex
        try:
            if msg.type == "content.from_change_package":
                await handle_content_from_change_package(payload=msg.payload)
            elif msg.type == "content.corporate_draft":
                await handle_content_corporate_draft(payload=msg.payload)
            else:
                json_log({"type": "worker_unknown_msg", "worker": "content", "request_id": rid, "msg_type": msg.type})
            await r.xack(STREAM_CONTENT_JOBS, GROUP_CONTENT, msg_id)
        except Exception as e:  # noqa: BLE001
            # Poison message protection: log and ACK so queue doesn't stall forever.
            json_log({"type": "worker_failed", "worker": "content", "request_id": rid, "msg_id": msg_id, "msg_type": msg.type, "error": str(e)})
            try:
                await r.xack(STREAM_CONTENT_JOBS, GROUP_CONTENT, msg_id)
            except Exception:
                pass
            await asyncio.sleep(1.0)


if __name__ == "__main__":
    asyncio.run(main())

