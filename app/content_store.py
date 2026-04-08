from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from app import config


ContentStatus = Literal["pending", "approved", "rejected", "needs_edit"]


@dataclass
class ContentItem:
    publication_id: str  # 5 digits as string
    created_at_utc: str
    status: ContentStatus
    title: str
    site_text: str
    vk_text: str
    internal_note: str
    sources: list[str]
    chief_last_decision_at_utc: str | None = None
    chief_explanation: str | None = None
    chief_message_id: str | None = None
    site_published_at_utc: str | None = None
    site_url: str | None = None
    vk_published_at_utc: str | None = None
    vk_post_id: int | None = None
    vk_post_url: str | None = None
    last_publish_error: str | None = None


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _item_path(publication_id: str) -> Path:
    return (config.CONTENT_ITEMS_DIR / f"{publication_id}.json").resolve()


def _archive_path(publication_id: str) -> Path:
    return (config.CONTENT_ARCHIVE_DIR / f"{publication_id}.json").resolve()


def next_publication_id() -> str:
    config.ensure_data_dirs()
    seq_path = config.CONTENT_SEQ_PATH
    seq_path.parent.mkdir(parents=True, exist_ok=True)
    if not seq_path.exists():
        seq_path.write_text("0", encoding="utf-8")
    raw = seq_path.read_text(encoding="utf-8").strip() or "0"
    try:
        n = int(raw)
    except ValueError:
        n = 0
    n += 1
    seq_path.write_text(str(n), encoding="utf-8")
    return f"{n:05d}"


def save_item(item: ContentItem) -> None:
    config.ensure_data_dirs()
    path = _item_path(item.publication_id)
    data = item.__dict__
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_item(publication_id: str) -> ContentItem:
    path = _item_path(publication_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    return ContentItem(**data)


def item_exists(publication_id: str) -> bool:
    return _item_path(publication_id).is_file()


def set_status(
    publication_id: str,
    *,
    status: ContentStatus,
    explanation: str | None = None,
    message_id: str | None = None,
) -> ContentItem:
    item = load_item(publication_id)
    item.status = status
    item.chief_last_decision_at_utc = _now_utc_iso()
    item.chief_explanation = explanation
    item.chief_message_id = message_id
    save_item(item)
    return item


def update_item(publication_id: str, **fields) -> ContentItem:
    item = load_item(publication_id)
    for k, v in fields.items():
        if not hasattr(item, k):
            continue
        setattr(item, k, v)
    save_item(item)
    return item


def archive_item(publication_id: str) -> None:
    src = _item_path(publication_id)
    if not src.exists():
        return
    dst = _archive_path(publication_id)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    src.unlink(missing_ok=True)


def purge_archived_older_than_days(days: int = 30) -> int:
    config.ensure_data_dirs()
    cutoff = time.time() - (days * 86400)
    removed = 0
    for p in config.CONTENT_ARCHIVE_DIR.glob("*.json"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
                removed += 1
        except Exception:
            continue
    return removed

