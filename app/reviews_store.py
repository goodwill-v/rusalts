"""Отзывы/кейсы клиентов на витрине (Этап C2).

shop.db, таблица reviews. Графика — файлы в data/uploads/reviews/ (защищено от
rsync), раздаются через /uploads/. Публикуются только approved.
Абстрактный стартовый контент сидируется при первом обращении (is_abstract=1),
Шеф заменит реальными кейсами.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app import config

UPLOAD_DIR = config.DATA_DIR / "uploads" / "reviews"
PUBLIC_PREFIX = "/uploads/reviews/"
_ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}
MAX_UPLOAD_BYTES = 5 * 1024 * 1024

SEED = [
    {
        "product_id": "italidia", "author": "Анна, студентка",
        "text": "Занимаюсь с ассистентом каждый день по дороге на учёбу. За месяц перестала бояться говорить — собеседник помнит мой уровень и подстраивается.",
        "rating": 5, "photo_url": "",
    },
    {
        "product_id": "vezhpom", "author": "Дмитрий, владелец интернет-магазина",
        "text": "Сайт упал ночью — узнал об этом не от клиента, а от ВежПома, через 2 минуты. Простои сократились с часов до минут.",
        "rating": 5, "photo_url": "",
    },
    {
        "product_id": "anketa_diagnostics", "author": "Сергей, директор консалтинговой компании",
        "text": "После аудита увидели, что 40% рутины можно передать ИИ. Дорожная карта из отчёта — рабочий документ, по нему и работаем.",
        "rating": 5, "photo_url": "",
    },
]


def _connect() -> sqlite3.Connection:
    db = sqlite3.connect(str(config.DATA_DIR / "shop.db"))
    db.row_factory = sqlite3.Row
    db.execute(
        """CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id TEXT NOT NULL DEFAULT '',
            author TEXT NOT NULL DEFAULT '',
            text TEXT NOT NULL DEFAULT '',
            rating INTEGER NOT NULL DEFAULT 5,
            photo_url TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'draft',
            is_abstract INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT ''
        )"""
    )
    count = db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
    if count == 0:
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        for r in SEED:
            db.execute(
                "INSERT INTO reviews (product_id, author, text, rating, photo_url,"
                " status, is_abstract, created_at) VALUES (?,?,?,?,?,?,1,?)",
                (r["product_id"], r["author"], r["text"], r["rating"],
                 r["photo_url"], "approved", now),
            )
        db.commit()
    return db


def list_reviews(*, include_hidden: bool = False, product_id: str = "") -> list[dict[str, Any]]:
    with closing(_connect()) as db:
        query = "SELECT * FROM reviews WHERE 1=1"
        params: list[Any] = []
        if not include_hidden:
            query += " AND status = 'approved'"
        if product_id:
            query += " AND product_id = ?"
            params.append(product_id)
        query += " ORDER BY id DESC"
        rows = db.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def upsert_review(data: dict[str, Any]) -> dict[str, Any]:
    rid = int(data.get("id") or 0)
    fields = {
        "product_id": str(data.get("product_id") or "").strip(),
        "author": str(data.get("author") or "").strip()[:120],
        "text": str(data.get("text") or "").strip()[:2000],
        "rating": min(5, max(1, int(data.get("rating") or 5))),
        "photo_url": str(data.get("photo_url") or "").strip()[:500],
        "status": data.get("status") if data.get("status") in ("approved", "draft") else "draft",
        "is_abstract": 1 if data.get("is_abstract") in (1, "1", True) else 0,
    }
    with closing(_connect()) as db:
        if rid:
            db.execute(
                "UPDATE reviews SET product_id=?, author=?, text=?, rating=?,"
                " photo_url=?, status=?, is_abstract=? WHERE id=?",
                (*fields.values(), rid),
            )
        else:
            fields["created_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            cols = ", ".join(fields)
            cur = db.execute(
                f"INSERT INTO reviews ({cols}) VALUES ({', '.join('?' * len(fields))})",
                tuple(fields.values()),
            )
            rid = cur.lastrowid
        db.commit()
        row = db.execute("SELECT * FROM reviews WHERE id = ?", (rid,)).fetchone()
    return dict(row)


def delete_review(rid: int) -> bool:
    with closing(_connect()) as db:
        cur = db.execute("DELETE FROM reviews WHERE id = ?", (rid,))
        db.commit()
    return cur.rowcount > 0


def save_photo(filename: str, content: bytes) -> str:
    """Сохранить картинку отзыва, вернуть публичный URL (/uploads/reviews/...)."""
    ext = Path(filename).suffix.lower()
    if ext not in _ALLOWED_EXT:
        raise ValueError(f"недопустимый формат: {ext}")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError("файл больше 5 МБ")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-z0-9_.-]", "", Path(filename).stem.lower())[:40] or "img"
    name = f"{safe}_{uuid.uuid4().hex[:8]}{ext}"
    (UPLOAD_DIR / name).write_bytes(content)
    return PUBLIC_PREFIX + name
