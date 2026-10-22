"""Лента диалогов АЛТ-эксперта (решение Шефа 22.10): полная история в админке,
краткие уведомления о лидах — в МАКС (max_notify).

БД: data/expert.db, таблица messages. Сессия = session_key (фронт генерирует
uuid в sessionStorage и шлёт как user_id). Лид — email/телефон в сообщении
пользователя (is_lead=1, одно МАКС-уведомление на сессию).
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

from app import config

DB_PATH = config.DATA_DIR / "expert.db"

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?:\+7|8)[\s(-]?\d{3}[\s)-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}")

LEAD_CHANNELS = {"email": _EMAIL_RE, "phone": _PHONE_RE}


def _connect() -> sqlite3.Connection:
    db = sqlite3.connect(str(config.DATA_DIR / "expert.db"))
    db.row_factory = sqlite3.Row
    db.execute(
        """CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_key TEXT NOT NULL,
            user_id TEXT NOT NULL DEFAULT '',
            channel TEXT NOT NULL DEFAULT '',
            platform TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL,
            text TEXT NOT NULL,
            is_lead INTEGER NOT NULL DEFAULT 0,
            lead_contact TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_expert_session ON messages(session_key, id)"
    )
    return db


def detect_lead(text: str) -> str:
    """Email или телефон в тексте — признак лида/эскалации."""
    m = _EMAIL_RE.search(text or "")
    if m:
        return m.group(0)
    m = _PHONE_RE.search(text or "")
    if m:
        return re.sub(r"\D", "", m.group(0))
    return ""


def log_message(
    *,
    session_key: str,
    role: str,
    text: str,
    user_id: str = "",
    channel: str = "",
    platform: str = "",
    is_lead: bool = False,
    lead_contact: str = "",
) -> int:
    with closing(_connect()) as db:
        cur = db.execute(
            "INSERT INTO messages (session_key, user_id, channel, platform, role,"
            " text, is_lead, lead_contact) VALUES (?,?,?,?,?,?,?,?)",
            (session_key[:80] or "anonymous", (user_id or "")[:80],
             (channel or "")[:40], (platform or "")[:40], role,
             (text or "")[:8000], 1 if is_lead else 0, lead_contact[:200]),
        )
        db.commit()
    return cur.lastrowid


def session_lead_notified(session_key: str) -> bool:
    """Уже было МАКС-уведомление по этой сессии (не спамить по каждому письму)."""
    with closing(_connect()) as db:
        row = db.execute(
            "SELECT COUNT(*) FROM messages WHERE session_key = ? AND is_lead = 1",
            (session_key,),
        ).fetchone()
    return bool(row[0])


def list_sessions(*, limit: int = 100, search: str = "", leads_only: bool = False) -> list[dict[str, Any]]:
    """Сессии: последняя реплика, счётчики, лид-признак."""
    query = """SELECT session_key,
                      MAX(created_at) AS last_at,
                      COUNT(*) AS msg_count,
                      SUM(CASE WHEN role='user' THEN 1 ELSE 0 END) AS user_count,
                      MAX(channel) AS channel,
                      MAX(is_lead) AS has_lead,
                      MAX(CASE WHEN is_lead = 1 THEN lead_contact ELSE '' END) AS lead_contact
               FROM messages WHERE 1=1"""
    params: list[Any] = []
    if search:
        query += " AND text LIKE ?"
        params.append(f"%{search}%")
    if leads_only:
        query += " AND session_key IN (SELECT DISTINCT session_key FROM messages WHERE is_lead = 1)"
    query += " GROUP BY session_key ORDER BY MAX(id) DESC LIMIT ?"
    params.append(limit)
    with closing(_connect()) as db:
        rows = db.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def get_session_messages(session_key: str, limit: int = 200) -> list[dict[str, Any]]:
    with closing(_connect()) as db:
        rows = db.execute(
            "SELECT role, text, is_lead, lead_contact, created_at"
            " FROM messages WHERE session_key = ? ORDER BY id LIMIT ?",
            (session_key, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def stats() -> dict[str, int]:
    with closing(_connect()) as db:
        total = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        sessions = db.execute("SELECT COUNT(DISTINCT session_key) FROM messages").fetchone()[0]
        leads = db.execute(
            "SELECT COUNT(DISTINCT session_key) FROM messages WHERE is_lead = 1"
        ).fetchone()[0]
    return {"messages": total, "sessions": sessions, "lead_sessions": leads}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
