from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone

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
        src = str(it.get("source_id") or "").strip()
        url = str(it.get("source_url") or "").strip()
        summary = str(it.get("summary") or it.get("title") or it.get("change") or "").strip()
        cls = str(it.get("classification") or "").strip()
        parts = [p for p in [summary, cls] if p]
        core = " — ".join(parts) if parts else (src or "изменение")
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
    vk_text = (f"{title}\n\n" + "\n".join([str(it.get("summary") or it.get("title") or "").strip() for it in (items or [])[:10] if (it.get('summary') or it.get('title'))])).strip()
    if sources:
        vk_text += "\n\nИсточники:\n" + "\n".join(sources[:5])
    used_model = "fallback(no_routerai)"
    return title, site_text, vk_text, sources, used_model


async def _generate_texts(*, change_package_path: str, items: list[dict]) -> tuple[str, str, str, list[str], str]:
    sources = sorted({str(it.get("source_url") or "").strip() for it in items if it.get("source_url")})
    sources = [s for s in sources if s]

    has_legal = _is_legal(items)
    choice = content_choice(has_legal=has_legal, main=config.CONTENT_MODEL_MAIN, heavy=config.CONTENT_MODEL_HEAVY)
    model = choice.model

    prompt = {
        "change_package_path": change_package_path,
        "items": items[:50],
        "rules": {
            "no_legal_advice": True,
            "must_cite_sources": True,
            "tone": "деловой, краткий, без сенсационности",
        },
        "output": {
            "title": "string (3..120)",
            "site_text_md": "string (markdown)",
            "vk_text": "string",
            "internal_note": "string",
        },
    }

    messages = [
        {
            "role": "system",
            "content": (
                "Ты агент Content проекта АЛТ-эксперт. "
                "Сформируй новость/релиз по change package. "
                "Если есть юридически чувствительные изменения — формулируй осторожно, "
                "не давай юридических советов, обязательно добавляй ссылки на источники."
            ),
        },
        {"role": "user", "content": f"Сгенерируй публикацию в JSON строго по схеме output.\n\nВходные данные:\n{json.dumps(prompt, ensure_ascii=False)}"},
    ]

    text, usage, _raw = await chat_completion(
        base_url=config.ROUTERAI_BASE_URL,
        api_key=config.ROUTERAI_API_KEY,
        model=model,
        messages=messages,
        timeout_s=35.0,
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

    # Best-effort JSON parse; fall back to plain text.
    title = "Обновления АЛТ‑эксперт"
    site_text = str(text).strip()
    vk_text = str(text).strip()
    internal_note = f"auto: generated from {change_package_path}"
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            title = str(obj.get("title") or title).strip()
            site_text = str(obj.get("site_text_md") or site_text).strip()
            vk_text = str(obj.get("vk_text") or vk_text).strip()
            internal_note = str(obj.get("internal_note") or internal_note).strip()
    except Exception:
        # Validation failed: retry once with heavy if configured and we weren't on heavy yet.
        if config.CONTENT_MODEL_HEAVY and model != config.CONTENT_MODEL_HEAVY:
            choice2 = content_choice(has_legal=True, main=config.CONTENT_MODEL_MAIN, heavy=config.CONTENT_MODEL_HEAVY)
            text2, usage2, _raw2 = await chat_completion(
                base_url=config.ROUTERAI_BASE_URL,
                api_key=config.ROUTERAI_API_KEY,
                model=choice2.model,
                messages=messages,
                timeout_s=45.0,
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
                    "routing_reason": "content_retry_json_parse_failed",
                }
            )
            try:
                obj2 = json.loads(text2)
                if isinstance(obj2, dict):
                    title = str(obj2.get("title") or title).strip()
                    site_text = str(obj2.get("site_text_md") or site_text).strip()
                    vk_text = str(obj2.get("vk_text") or vk_text).strip()
                    internal_note = str(obj2.get("internal_note") or internal_note).strip()
                    model = choice2.model
            except Exception:
                # Keep plain-text fallback
                pass

    return title, site_text, vk_text, sources, model


async def handle_content_from_change_package(*, payload: dict) -> None:
    config.ensure_data_dirs()
    change_package_path = str(payload.get("change_package_path") or "").strip()
    items = payload.get("items") or []
    if not isinstance(items, list):
        items = []

    used_model = ""
    err_s = ""
    try:
        title, site_text, vk_text, sources, used_model = await _generate_texts(change_package_path=change_package_path, items=items)
    except RouterAIError as e:
        err_s = str(e) or "RouterAI request failed"
        title, site_text, vk_text, sources, used_model = _fallback_publication(change_package_path=change_package_path, items=items, error=err_s)
    except Exception as e:  # noqa: BLE001
        err_s = str(e) or "content generation failed"
        title, site_text, vk_text, sources, used_model = _fallback_publication(change_package_path=change_package_path, items=items, error=err_s)

    pub_id = next_publication_id()
    item = ContentItem(
        publication_id=pub_id,
        created_at_utc=_now_utc_iso(),
        status="pending",
        title=title,
        site_text=site_text,
        vk_text=vk_text,
        internal_note=(f"{payload.get('ts_utc') or ''} | model={used_model}\n" + (payload.get("internal_note") or "")).strip(),
        sources=sources,
        last_publish_error=(err_s or None),
    )
    save_item(item)

    # For server automation we default to local_autoapprove; can be switched to email later.
    if config.CONTENT_APPROVAL_MODE == "local_autoapprove":
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

