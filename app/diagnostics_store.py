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
    with closing(_connect()) as db:
        cur = db.execute(
            "INSERT INTO diagnostics (kind, name, email, phone, company, answers, result) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                kind, name, email, phone, company,
                json.dumps(answers or {}, ensure_ascii=False),
                json.dumps(result or {}, ensure_ascii=False),
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
