"""История диалогов «Алик» (таб /zcode) — SQLite на бэкенде (ТЗ-дополнение §3).

Chat Completions API stateless: openclaw хранит контекст у себя по sessionKey,
но UI не может получить прошлые сообщения обратно. Поэтому история диалогов
живёт ЗДЕСЬ (решение Шефа): Шеф заходит с телефона и десктопа — история
обязана быть общей, localStorage запрещён ТЗ.

Это хранилище НЕ связано с историей zcode-сессий (те — из транскрипта zcode).
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from contextlib import closing
from typing import Any

from app import config

# Последние ~30 сообщений диалога уходят в запрос к gateway (ТЗ §3:
# защита от роста контекста и стоимости).
CONTEXT_LIMIT = 30


def _connect() -> sqlite3.Connection:
    db = sqlite3.connect(str(config.DATA_DIR / "alik_chat.db"))
    db.row_factory = sqlite3.Row
    db.execute(
        """CREATE TABLE IF NOT EXISTS alik_chat (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            ts REAL NOT NULL
        )"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_alik_chat_chat ON alik_chat(chat_id, id)")
    return db


def new_chat_id() -> str:
    return uuid.uuid4().hex[:12]


def add_message(chat_id: str, role: str, content: str, ts: float | None = None) -> int:
    """Записать сообщение, вернуть id строки (для отката при сбое gateway)."""
    with closing(_connect()) as db, db:
        cur = db.execute(
            "INSERT INTO alik_chat (chat_id, role, content, ts) VALUES (?, ?, ?, ?)",
            (chat_id, role, content, ts if ts is not None else time.time()),
        )
        return int(cur.lastrowid or 0)


def delete_message(row_id: int) -> None:
    """Откат сообщения при сбое gateway (чтобы ретрай не дублировал)."""
    with closing(_connect()) as db, db:
        db.execute("DELETE FROM alik_chat WHERE id = ?", (row_id,))


def history(chat_id: str, limit: int = 500) -> list[dict[str, Any]]:
    """Сообщения чата по возрастанию времени (для отрисовки)."""
    with closing(_connect()) as db:
        rows = db.execute(
            "SELECT id, role, content, ts FROM alik_chat WHERE chat_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (chat_id, int(limit)),
        ).fetchall()
    return [
        {"id": r["id"], "role": r["role"], "content": r["content"], "ts": r["ts"]}
        for r in reversed(rows)
    ]


def context_messages(chat_id: str, limit: int = CONTEXT_LIMIT) -> list[dict[str, str]]:
    """Последние `limit` сообщений по возрастанию — массив messages для gateway."""
    with closing(_connect()) as db:
        rows = db.execute(
            "SELECT role, content FROM alik_chat WHERE chat_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (chat_id, int(limit)),
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def chats() -> list[dict[str, Any]]:
    """Список чатов: id, название (первое сообщение пользователя), время последней активности."""
    with closing(_connect()) as db:
        rows = db.execute(
            """SELECT c.chat_id AS chat_id,
                      MIN(c.id) AS first_id,
                      COUNT(c.id) AS messages,
                      MAX(c.ts) AS last_ts
               FROM alik_chat c
               GROUP BY c.chat_id"""
        ).fetchall()
        result = []
        for r in rows:
            first_user = db.execute(
                "SELECT content FROM alik_chat WHERE chat_id = ? AND role = 'user' "
                "ORDER BY id ASC LIMIT 1",
                (r["chat_id"],),
            ).fetchone()
            title = (first_user["content"] if first_user else "").strip()
            result.append(
                {
                    "chat_id": r["chat_id"],
                    "title": title[:80] if title else "Новый чат",
                    "messages": r["messages"],
                    "last_ts": r["last_ts"],
                }
            )
    result.sort(key=lambda c: c["last_ts"], reverse=True)
    return result
