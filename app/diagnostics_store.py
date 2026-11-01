"""Хранилище диагностик (ТЗ 5.4): SQLite data/diagnostics.db, таблица diagnostics.

Анкеты: free («Бесплатная диагностика»), your_business («Ваше дело»),
paid («Анализ процессов предприятия», 3 000 ₽).
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from typing import Any

from app import config

DB_PATH = config.DATA_DIR / "diagnostics.db"
REPORTS_DIR = config.DATA_DIR / "diagnostics_reports"

KINDS = {"free", "your_business", "paid", "expert_lead"}


def _connect() -> sqlite3.Connection:
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute(
        """CREATE TABLE IF NOT EXISTS diagnostics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            email TEXT NOT NULL DEFAULT '',
            phone TEXT NOT NULL DEFAULT '',
            company TEXT NOT NULL DEFAULT '',
            answers TEXT NOT NULL DEFAULT '{}',
            result TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'new',
            pdf_path TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    # обратная совместимость БД, созданной до появления result/followup
    cols = {r[1] for r in db.execute("PRAGMA table_info(diagnostics)").fetchall()}
    if "result" not in cols:
        db.execute("ALTER TABLE diagnostics ADD COLUMN result TEXT NOT NULL DEFAULT ''")
    if "followup_stage" not in cols:
        db.execute("ALTER TABLE diagnostics ADD COLUMN followup_stage INTEGER NOT NULL DEFAULT 0")
    if "unsubscribed" not in cols:
        db.execute("ALTER TABLE diagnostics ADD COLUMN unsubscribed INTEGER NOT NULL DEFAULT 0")
    if "consent_at" not in cols:
        db.execute("ALTER TABLE diagnostics ADD COLUMN consent_at TEXT NOT NULL DEFAULT ''")
    return db


def save_diagnostic(
    kind: str,
    *,
    name: str = "",
    email: str = "",
    phone: str = "",
    company: str = "",
    answers: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
) -> int:
    if kind not in KINDS:
        raise ValueError(f"неизвестный тип диагностики: {kind}")
    from datetime import datetime, timezone

    consent_at = (
        datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        if (answers or {}).get("consent") else ""
    )
    with closing(_connect()) as db:
        cur = db.execute(
            "INSERT INTO diagnostics (kind, name, email, phone, company, answers, result, consent_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                kind, name, email, phone, company,
                json.dumps(answers or {}, ensure_ascii=False),
                json.dumps(result or {}, ensure_ascii=False),
                consent_at,
            ),
        )
        db.commit()
    return int(cur.lastrowid)


def set_pdf_path(diag_id: int, path: str) -> None:
    with closing(_connect()) as db:
        db.execute("UPDATE diagnostics SET pdf_path = ? WHERE id = ?", (path, diag_id))
        db.commit()


def list_diagnostics(limit: int = 200) -> list[dict[str, Any]]:
    with closing(_connect()) as db:
        rows = db.execute(
            "SELECT id, kind, name, email, phone, company, result, status, pdf_path, created_at "
            "FROM diagnostics ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        try:
            d["result"] = json.loads(d.get("result") or "{}")
        except ValueError:
            d["result"] = {}
        items.append(d)
    return items


def set_followup_stage(diag_id: int, stage: int) -> None:
    with closing(_connect()) as db:
        db.execute(
            "UPDATE diagnostics SET followup_stage = ? WHERE id = ?", (stage, diag_id)
        )
        db.commit()


def set_unsubscribed(diag_id: int) -> None:
    with closing(_connect()) as db:
        db.execute(
            "UPDATE diagnostics SET unsubscribed = 1 WHERE id = ?", (diag_id,)
        )
        db.commit()


def is_unsubscribed(diag_id: int) -> bool:
    with closing(_connect()) as db:
        row = db.execute(
            "SELECT unsubscribed FROM diagnostics WHERE id = ?", (diag_id,)
        ).fetchone()
    return bool(row and row["unsubscribed"])


def list_followup_candidates(*, stage: int, min_age_days: int, limit: int = 50) -> list[dict[str, Any]]:
    """Заявки для письма цепочки: нужная стадия, возраст ≥ N дней, не отписаны."""
    with closing(_connect()) as db:
        rows = db.execute(
            """SELECT id, kind, name, email, followup_stage, unsubscribed,
                      created_at,
                      CAST(strftime('%s','now') - strftime('%s', created_at) AS INTEGER) AS age_days
               FROM diagnostics
               WHERE followup_stage = ? AND unsubscribed = 0
                 AND email != ''
                 AND CAST(strftime('%s','now') - strftime('%s', created_at) AS INTEGER) >= ?
               ORDER BY id LIMIT ?""",
            (stage, min_age_days * 86400, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def cleanup_expired(*, anketa_months: int = 12, chat_months: int = 6) -> dict[str, int]:
    """Удаление заявок и диалогов старше сроков хранения (вызывается из followup-цикла)."""
    deleted = {"diagnostics": 0, "expert": 0}
    with closing(_connect()) as db:
        cur = db.execute(
            "DELETE FROM diagnostics WHERE created_at < datetime('now', ?)",
            (f"-{anketa_months} months",),
        )
        deleted["diagnostics"] = cur.rowcount
        db.commit()
    try:
        import sqlite3 as _s
        with closing(_s.connect(str(config.DATA_DIR / "expert.db"))) as db:
            cur = db.execute(
                "DELETE FROM messages WHERE created_at < datetime('now', ?)",
                (f"-{chat_months} months",),
            )
            deleted["expert"] = cur.rowcount
            db.commit()
    except _s.Error:
        pass
    return deleted


def log_subject_request(*, contact: str, action: str, status: str = "open",
                        note: str = "") -> int:
    """Журнал запросов субъектов ПДн (ст. 20 152-ФЗ)."""
    with closing(_connect()) as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS subject_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contact TEXT NOT NULL,
                action TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )"""
        )
        cur = db.execute(
            "INSERT INTO subject_requests (contact, action, status, note) VALUES (?,?,?,?)",
            (contact, action, status, note),
        )
        db.commit()
    return cur.lastrowid


def list_subject_requests(limit: int = 100) -> list[dict[str, Any]]:
    with closing(_connect()) as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS subject_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contact TEXT NOT NULL,
                action TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )"""
        )
        rows = db.execute(
            "SELECT * FROM subject_requests ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def purge_subject(email_or_phone: str) -> dict[str, int]:
    """Удаление/обезличивание данных субъекта по запросу (кнопка в админке)."""
    key = (email_or_phone or "").strip().lower()
    if not key:
        return {"deleted": 0}
    like = f"%{key}%"
    removed = {}
    with closing(_connect()) as db:
        cur = db.execute(
            "DELETE FROM diagnostics WHERE lower(email) LIKE ? OR lower(phone) LIKE ?",
            (like, like),
        )
        removed["diagnostics"] = cur.rowcount
        db.commit()
    import sqlite3 as _s
    for dbfile, table in (("expert.db", "messages"),):
        try:
            with closing(_s.connect(str(config.DATA_DIR / dbfile))) as db:
                cur = db.execute(
                    f"DELETE FROM {table} WHERE lower(text) LIKE ?", (like,)
                )
                removed[dbfile] = cur.rowcount
                db.commit()
        except _s.Error:
            pass
    from app.reviews_store import list_reviews, delete_review
    hidden = 0
    for r in list_reviews(include_hidden=True):
        if key in (r.get("author") or "").lower():
            delete_review(r["id"])
            hidden += 1
    removed["reviews"] = hidden
    return removed
