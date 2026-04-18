from __future__ import annotations

import json
from datetime import datetime, timezone

from app import config
from app.content_excerpt import title_fallback_from_site_text
from app.content_store import load_item, set_status, update_item
from app.markdown_plain import strip_markdown_public
from app.observability import json_log
from app.publishers.site import publish_to_site
from app.publishers.vk import publish_to_vk


def _append_feedback(*, publication_id: str, payload: dict) -> None:
    try:
        config.ensure_data_dirs()
        path = (config.MONITORING_DIR / "content_approvals_feedback.jsonl").resolve()
        line = json.dumps({"publication_id": publication_id, **payload}, ensure_ascii=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


async def approve_publication_by_id(*, request_id: str, publication_id: str) -> dict:
    """
    Одобрить и опубликовать материал (сайт + ВК). Общая логика для роутера и content-worker.
    Возвращает поля для ApproveResponse.
    """
    set_status(publication_id, status="approved", message_id="web_approved")
    it = load_item(publication_id)

    site_c = strip_markdown_public(it.site_text or "")
    vk_c = strip_markdown_public((it.vk_text or "").strip() or site_c)
    if len(vk_c) < 10:
        vk_c = site_c
    title_c = title_fallback_from_site_text(site_c)
    if (
        site_c != (it.site_text or "").strip()
        or vk_c != (it.vk_text or "").strip()
        or title_c != (it.title or "").strip()
    ):
        update_item(publication_id, site_text=site_c, vk_text=vk_c, title=title_c)
        it = load_item(publication_id)

    _append_feedback(
        publication_id=publication_id,
        payload={
            "approved_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "title": it.title,
            "site_text": it.site_text,
            "vk_text": it.vk_text,
            "sources": it.sources,
            "pinned": bool(getattr(it, "pinned", False)),
        },
    )

    site_published = False
    vk_published = False
    if not it.site_published_at_utc:
        pub_at, url_path = publish_to_site(it)
        update_item(publication_id, site_published_at_utc=pub_at, site_url=url_path, last_publish_error=None)
        site_published = True

    it = load_item(publication_id)
    if not it.vk_published_at_utc:
        try:
            pub_at, post_id, post_url = await publish_to_vk(it)
            update_item(
                publication_id,
                vk_published_at_utc=pub_at,
                vk_post_id=post_id,
                vk_post_url=post_url,
                last_publish_error=None,
            )
            vk_published = True
        except Exception as e:  # noqa: BLE001
            update_item(publication_id, last_publish_error=str(e))

    it2 = load_item(publication_id)
    json_log(
        {
            "type": "content_approved_web",
            "request_id": request_id,
            "publication_id": publication_id,
            "site_published": site_published,
            "vk_published": vk_published,
            "last_publish_error": it2.last_publish_error,
        }
    )
    return {
        "ok": True,
        "request_id": request_id,
        "publication_id": publication_id,
        "site_published": site_published or bool(it2.site_published_at_utc),
        "vk_published": vk_published or bool(it2.vk_published_at_utc),
        "vk_post_url": it2.vk_post_url,
        "last_publish_error": it2.last_publish_error,
    }
