from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from app import config
from app.content_store import ContentItem, load_item, next_publication_id, save_item, set_status, update_item
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
    """
    Публичные поля не должны содержать JSON/служебные инструкции.
    Убираем fenced-json куски и тройные кавычки, нормализуем пробелы.
    """
    t = (s or "").strip()
    if not t:
        return ""
    # Remove fenced json blocks entirely
    t = _FENCED_JSON_RE.sub("", t)
    # Remove accidental codefence leftovers
    t = t.replace("```", "").strip()
    # Drop obvious internal-note markers
    t = re.sub(r"(?im)^\s*internal_note\s*[:=].*$", "", t).strip()
    # Normalize whitespace
    t = "\n".join(line.rstrip() for line in t.splitlines()).strip()
    return t


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

    has_legal = _is_legal(items_aug)
    choice = content_choice(has_legal=has_legal, main=config.CONTENT_MODEL_MAIN, heavy=config.CONTENT_MODEL_HEAVY)
    model = choice.model

    sources_for_prompt = [{"label": _domain_label(u), "url": u} for u in sources[:20]]

    prompt = {
        "change_package_path": change_package_path,
        "items": items_aug[:40],
        "sources": sources_for_prompt,
        "rules": {
            "no_legal_advice": True,
            "must_cite_sources": True,
            "tone_site": "официально-деловой, экспертный, нейтральный",
            "tone_vk": "динамичный, вовлекающий, экспертный но дружеский",
            "no_technical_notes_in_public_text": True,
        },
        "output": {
            "title": "string (3..120)",
            "site_text_md": "string (markdown)",
            "vk_text": "string",
            "internal_note": "string (PRIVATE, not for publication)",
        },
    }

    messages = [
        {
            "role": "system",
            "content": (
                "Ты агент Content проекта АЛТ-эксперт. "
                "Сформируй литературный публицистический текст по change package для двух каналов: Сайт и ВКонтакте. "
                "Нельзя публиковать JSON, служебные инструкции или внутренние заметки. "
                "Если есть юридически чувствительные изменения — формулируй осторожно, "
                "не давай юридических советов, обязательно добавляй ссылки на первоисточники."
            ),
        },
        {
            "role": "user",
            "content": (
                "Сгенерируй публикацию строго в виде ОДНОГО JSON-объекта, без ```json``` и без любого текста вокруг.\n"
                "Требования к результату:\n"
                "- Определи: это один «Релиз» (если много мелких равнозначных пунктов) или одна «Новость» (если есть существенная тема).\n"
                "- Заголовок:\n"
                "  - для релиза начинается с: «Релиз \\\"...\\\"»\n"
                "  - для новости: коротко отражает суть.\n"
                "- САЙТ (`site_text_md`): официально-деловой экспертный стиль, без эмодзи. Текст как статья/новость.\n"
                "  - Если источник один: в конце отдельной строкой «Источник: <label> — <url>».\n"
                "  - Если источников несколько: ссылки в тексте упакуй в названия ресурсов (Markdown-ссылки вида [rustore.ru](URL)).\n"
                "  - Для официальных регуляторных тем обязателен хотя бы один официальный источник (pravo.gov.ru, rkn.gov.ru и т.п.), если он есть во входных данных.\n"
                "- ВК (`vk_text`): динамично, абзацы по 2–3 строки, эмодзи-акценты умеренно (🔹✅⚠️), главное — в первом абзаце.\n"
                "  - Первый абзац — это анонс до 200 знаков.\n"
                "  - В конце: призыв к действию и хэштеги #АЛТ #МАХ #ЗаконыИТ.\n"
                "  - Ссылки упаковывай в название ресурса: «rustore.ru — https://...», без голых ссылок посреди текста.\n"
                "- `internal_note`: только для редактора/аналитики, НЕ писать сюда инструкций вроде «тон выдержан…», только факты о том, что обобщалось.\n\n"
                f"Входные данные:\n{json.dumps(prompt, ensure_ascii=False)}"
            ),
        },
    ]

    text, usage, _raw = await chat_completion(
        base_url=config.ROUTERAI_BASE_URL,
        api_key=config.ROUTERAI_API_KEY,
        model=model,
        messages=messages,
        timeout_s=60.0,
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
                timeout_s=90.0,
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

    title = str(obj.get("title") or title).strip()
    site_text = _clean_public_text(str(obj.get("site_text_md") or "")).strip()
    vk_text = _clean_public_text(str(obj.get("vk_text") or "")).strip()
    internal_note = str(obj.get("internal_note") or internal_note).strip()

    # Minimal validation: public texts must not be empty and must not contain JSON braces-only dumps.
    if not site_text or not vk_text:
        raise ValueError("LLM returned empty public text fields")
    if site_text.lstrip().startswith("{") or vk_text.lstrip().startswith("{"):
        raise ValueError("LLM returned JSON-like public text")

    return title, site_text, vk_text, sources, model


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
        internal_note=(f"{payload.get('ts_utc') or ''} | model={used_model}\n" + (payload.get("internal_note") or "")).strip(),
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

