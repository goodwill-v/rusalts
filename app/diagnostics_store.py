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

KINDS = {"free", "your_business", "paid"}


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
    # обратная совместимость БД, созданной до появления result
    cols = {r[1] for r in db.execute("PRAGMA table_info(diagnostics)").fetchall()}
    if "result" not in cols:
        db.execute("ALTER TABLE diagnostics ADD COLUMN result TEXT NOT NULL DEFAULT ''")
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
