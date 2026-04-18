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


def _reorder_index(idx: list[dict]) -> list[dict]:
    """Pinned first (newest first), then normal (newest first)."""
    pinned = [x for x in idx if x.get("pinned")]
    normal = [x for x in idx if not x.get("pinned")]
    pinned.sort(key=lambda x: str(x.get("published_at_utc") or ""), reverse=True)
    normal.sort(key=lambda x: str(x.get("published_at_utc") or ""), reverse=True)
    return pinned + normal


def remove_site_publications(publication_ids: list[str]) -> tuple[int, list[str]]:
    """
    Remove entries from site index and delete markdown files.
    Returns (removed_index_entries_count, list of ids that had an index row removed).
    """
    idset = {str(x).strip() for x in publication_ids if str(x).strip()}
    if not idset:
        return 0, []
    idx = _load_index()
    removed_rows: list[str] = []
    kept: list[dict] = []
    for row in idx:
        pid = str(row.get("publication_id") or "").strip()
        if pid in idset:
            removed_rows.append(pid)
        else:
            kept.append(row)

    base = config.CONTENT_PUBLISHED_SITE_DIR.resolve()
    for pid in idset:
        md = (config.CONTENT_PUBLISHED_SITE_DIR / f"{pid}.md").resolve()
        try:
            md.relative_to(base)
        except ValueError:
            continue
        if md.is_file():
            md.unlink(missing_ok=True)

    _save_index(_reorder_index(kept)[:500])
    return len(removed_rows), removed_rows


def set_site_publications_pinned(publication_ids: list[str], pinned: bool) -> int:
    """Update pinned flag for given publication ids in site index. Returns count updated."""
    idset = {str(x).strip() for x in publication_ids if str(x).strip()}
    if not idset:
        return 0
    idx = _load_index()
    n = 0
    for row in idx:
        pid = str(row.get("publication_id") or "").strip()
        if pid in idset:
            row["pinned"] = bool(pinned)
            n += 1
    _save_index(_reorder_index(idx)[:500])
    return n


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
        "pinned": bool(getattr(item, "pinned", False)),
    }
    replaced = False
    for i, it in enumerate(idx):
        if str(it.get("publication_id")) == item.publication_id:
            idx[i] = meta
            replaced = True
            break
    if not replaced:
        idx.insert(0, meta)

    idx = _reorder_index(idx)

    _save_index(idx[:500])
    return pub_at, url_path


def get_site_markdown(publication_id: str) -> str:
    path = (config.CONTENT_PUBLISHED_SITE_DIR / f"{publication_id}.md").resolve()
    if not path.is_file():
        raise FileNotFoundError(publication_id)
    return path.read_text(encoding="utf-8")

