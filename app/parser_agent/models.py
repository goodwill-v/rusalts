from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Source:
    id: str
    title: str
    url: str
    priority: str
    level: int
    content_type: str
    frequency: str
    status: str


@dataclass(frozen=True)
class FetchResult:
    source: Source
    status: int
    final_url: str
    etag: str | None
    last_modified: str | None
    content_type: str | None
    text: str


@dataclass(frozen=True)
class ChangeItem:
    item_id: str
    ts_utc: str
    source_id: str
    source_title: str
    source_url: str
    classification: str
    section_path: str
    article_id: str
    article_path: str
    summary: str
    links: list[str]

