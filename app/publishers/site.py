from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from app import config
from app.content_store import ContentItem


def _now_utc_iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load_index() -> list[dict]:
    path = config.CONTENT_PUBLISHED_SITE_INDEX_PATH
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_index(items: list[dict]) -> None:
    path = config.CONTENT_PUBLISHED_SITE_INDEX_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def publish_to_site(item: ContentItem) -> tuple[str, str]:
    """
    Publishes item to local storage and returns (published_at_utc, url_path).
    URL is served via API endpoint, not static Nginx root.
    """
    config.ensure_data_dirs()
    pub_at = _now_utc_iso_z()
    md_path = (config.CONTENT_PUBLISHED_SITE_DIR / f"{item.publication_id}.md").resolve()
    md_path.write_text(item.site_text.strip() + "\n", encoding="utf-8")

    url_path = f"/api/content/site/{item.publication_id}"

    idx = _load_index()
    # upsert by publication_id
    meta = {
        "publication_id": item.publication_id,
        "title": item.title,
        "published_at_utc": pub_at,
        "url": url_path,
        "sources": item.sources,
    }
    replaced = False
    for i, it in enumerate(idx):
        if str(it.get("publication_id")) == item.publication_id:
            idx[i] = meta
            replaced = True
            break
    if not replaced:
        idx.insert(0, meta)
    _save_index(idx[:500])
    return pub_at, url_path


def get_site_markdown(publication_id: str) -> str:
    path = (config.CONTENT_PUBLISHED_SITE_DIR / f"{publication_id}.md").resolve()
    if not path.is_file():
        raise FileNotFoundError(publication_id)
    return path.read_text(encoding="utf-8")

