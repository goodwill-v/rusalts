"""Хранилище «Свежих публикаций» страницы /channels/ (ТЗ МАКа 21.11).

Новая БД data/publications.db — существующие БД не трогает. Поля:
заголовок (≤100), полный текст (HTML, админский доверенный), картинка-URL
(опционально), автор/рубрика, дата публикации (сортировка), закреп (≤3,
валидация), показывать (черновики скрыты). Миграция идемпотентная (CREATE
TABLE IF NOT EXISTS) — бэкап до первой миграции не требуется: БД новая.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

from app import config

DB_PATH = config.DATA_DIR / "publications.db"
MAX_PINNED = 3
MAX_TITLE = 100


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute(
        """CREATE TABLE IF NOT EXISTS publications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL DEFAULT '',
            body_html TEXT NOT NULL DEFAULT '',
            image_url TEXT NOT NULL DEFAULT '',
            author TEXT NOT NULL DEFAULT '',
            published_at TEXT NOT NULL DEFAULT '',
            pinned INTEGER NOT NULL DEFAULT 0,
            visible INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        )"""
    )
    return db


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _row_to_dict(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["pinned"] = bool(d["pinned"])
    d["visible"] = bool(d["visible"])
    return d


def count_pinned(exclude_id: int | None = None) -> int:
    q = "SELECT COUNT(*) FROM publications WHERE pinned = 1"
    params: tuple = ()
    if exclude_id:
        q += " AND id != ?"
        params = (exclude_id,)
    with closing(_connect()) as db:
        return db.execute(q, params).fetchone()[0]


def validate(title: str, pinned: bool, exclude_id: int | None = None) -> str | None:
    """Возвращает текст ошибки или None. Вызывается до записи."""
    if not title.strip():
        return "Заголовок обязателен"
    if len(title.strip()) > MAX_TITLE:
        return f"Заголовок — не более {MAX_TITLE} знаков"
    if pinned and count_pinned(exclude_id) >= MAX_PINNED:
        return f"Закреплённых уже {MAX_PINNED} — снимите закреп с одной из них"
    return None


def save(data: dict[str, Any], pub_id: int | None = None) -> int:
    """Создать (pub_id=None) или обновить публикацию. Возвращает id."""
    now = _now()
    with closing(_connect()) as db:
        if pub_id is None:
            cur = db.execute(
                """INSERT INTO publications
                   (title, body_html, image_url, author, published_at,
                    pinned, visible, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    data.get("title", ""), data.get("body_html", ""),
                    data.get("image_url", ""), data.get("author", ""),
                    data.get("published_at", ""), int(data.get("pinned", 0)),
                    int(data.get("visible", 1)), now, now,
                ),
            )
            db.commit()
            return int(cur.lastrowid)
        db.execute(
            """UPDATE publications SET title=?, body_html=?, image_url=?, author=?,
               published_at=?, pinned=?, visible=?, updated_at=? WHERE id=?""",
            (
                data.get("title", ""), data.get("body_html", ""),
                data.get("image_url", ""), data.get("author", ""),
                data.get("published_at", ""), int(data.get("pinned", 0)),
                int(data.get("visible", 1)), now, pub_id,
            ),
        )
        db.commit()
        return pub_id


def get(pub_id: int) -> dict[str, Any] | None:
    with closing(_connect()) as db:
        r = db.execute("SELECT * FROM publications WHERE id = ?", (pub_id,)).fetchone()
    return _row_to_dict(r) if r else None


def list_all() -> list[dict[str, Any]]:
    """Все публикации (для админки): закреплённые сверху, далее по дате убыв."""
    with closing(_connect()) as db:
        rows = db.execute(
            "SELECT * FROM publications ORDER BY pinned DESC, published_at DESC, id DESC"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_visible(limit: int = 10) -> list[dict[str, Any]]:
    """Лента /channels/: закреплённые сверху (до 3, по дате), далее остальные
    по дате убыв. Только показывать=true."""
    with closing(_connect()) as db:
        rows = db.execute(
            "SELECT * FROM publications WHERE visible = 1 ORDER BY pinned DESC, published_at DESC, id DESC LIMIT ?",
            (int(limit) * 2,),
        ).fetchall()
    items = [_row_to_dict(r) for r in rows]
    pinned = [it for it in items if it["pinned"]][:MAX_PINNED]
    rest = [it for it in items if not it["pinned"]]
    return (pinned + rest)[:limit]


def delete(pub_id: int) -> bool:
    with closing(_connect()) as db:
        cur = db.execute("DELETE FROM publications WHERE id = ?", (pub_id,))
        db.commit()
    return cur.rowcount > 0
