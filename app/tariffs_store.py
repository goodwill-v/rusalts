"""Тарифы в БД (Этап A2 ТЗ Развития): единый источник правды для админки и API.

shop.db, таблица tariffs. При первом обращении сидируется из billing.TARIFFS (код = фолбэк).
Чтение — с in-process TTL-кешем 60 с (правки из админки применяются ≤60 с, без рестартов).
"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

from app import config

TTL_SECONDS = 60
_LOCK = threading.Lock()
_cache: dict[str, Any] = {"at": 0.0, "rows": None}

TARIFF_FIELDS = (
    "price_rub", "period_days", "trial_days", "token_allowance", "trial_tokens",
    "model", "prompt_price_per_1m", "completion_price_per_1m",
    "token_cost_ratio", "additional_token_price_per_1m", "estimated_dialogues", "name",
)


def _connect() -> sqlite3.Connection:
    db = sqlite3.connect(str(config.DATA_DIR / "shop.db"))
    db.row_factory = sqlite3.Row
    db.execute(
        """CREATE TABLE IF NOT EXISTS tariffs (
            product_id TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            price_rub INTEGER NOT NULL DEFAULT 0,
            period_days INTEGER NOT NULL DEFAULT 30,
            trial_days INTEGER NOT NULL DEFAULT 0,
            token_allowance INTEGER NOT NULL DEFAULT 0,
            trial_tokens INTEGER NOT NULL DEFAULT 0,
            model TEXT NOT NULL DEFAULT '',
            prompt_price_per_1m REAL NOT NULL DEFAULT 0,
            completion_price_per_1m REAL NOT NULL DEFAULT 0,
            token_cost_ratio REAL NOT NULL DEFAULT 0.6,
            additional_token_price_per_1m REAL NOT NULL DEFAULT 0,
            estimated_dialogues TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        )"""
    )
    return db


def _seed_if_empty(db: sqlite3.Connection) -> None:
    count = db.execute("SELECT COUNT(*) FROM tariffs").fetchone()[0]
    if count:
        return
    from app.billing import TARIFFS

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    for pid, t in TARIFFS.items():
        if pid.endswith("_test"):
            continue
        db.execute(
            "INSERT OR IGNORE INTO tariffs (product_id, name, price_rub, period_days,"
            " trial_days, token_allowance, trial_tokens, model, prompt_price_per_1m,"
            " completion_price_per_1m, token_cost_ratio, additional_token_price_per_1m,"
            " estimated_dialogues, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                pid, t.name, t.price_rub,
                _product_period(pid), _product_trial_days(pid),
                t.token_allowance, t.trial_tokens, t.routerai_model_id,
                t.routerai_prompt_price_per_1m, t.routerai_completion_price_per_1m,
                t.token_cost_ratio, t.additional_token_price_per_1m,
                t.estimated_dialogues, now,
            ),
        )
    db.commit()


def _product_period(pid: str) -> int:
    from app.payments import PRODUCTS

    p = PRODUCTS.get(pid)
    return int(p.get("period_days") or 30) if p else 30


def _product_trial_days(pid: str) -> int:
    from app.payments import PRODUCTS

    p = PRODUCTS.get(pid)
    return int(p.get("trial_days") or 0) if p else 0


def invalidate_cache() -> None:
    with _LOCK:
        _cache["at"] = 0.0
        _cache["rows"] = None


def list_tariffs(*, force: bool = False) -> list[dict[str, Any]]:
    now = time.monotonic()
    if not force and _cache["rows"] is not None and now - _cache["at"] < TTL_SECONDS:
        return _cache["rows"]
    with closing(_connect()) as db:
        _seed_if_empty(db)
        rows = db.execute(
            "SELECT product_id, name, price_rub, period_days, trial_days,"
            " token_allowance, trial_tokens, model, prompt_price_per_1m,"
            " completion_price_per_1m, token_cost_ratio, additional_token_price_per_1m,"
            " estimated_dialogues, updated_at FROM tariffs ORDER BY product_id"
        ).fetchall()
    items = [dict(r) for r in rows]
    with _LOCK:
        _cache["at"] = now
        _cache["rows"] = items
    return items


def get_tariff_row(product_id: str) -> dict[str, Any] | None:
    for row in list_tariffs():
        if row["product_id"] == product_id:
            return row
    return None


def upsert_tariffs(values: dict[str, dict[str, Any]]) -> int:
    """Массовое сохранение: {product_id: {field: value}}. Возвращает число обновлённых."""
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    saved = 0
    numeric_int = {"price_rub", "period_days", "trial_days", "token_allowance", "trial_tokens"}
    numeric_float = {
        "prompt_price_per_1m", "completion_price_per_1m",
        "token_cost_ratio", "additional_token_price_per_1m",
    }
    with closing(_connect()) as db:
        _seed_if_empty(db)
        for pid, fields in values.items():
            if not isinstance(fields, dict):
                continue
            sets, params = [], []
            for k, v in fields.items():
                if k not in TARIFF_FIELDS:
                    continue
                if k in numeric_int:
                    v = max(0, int(v or 0))
                elif k in numeric_float:
                    v = float(v or 0)
                else:
                    v = str(v or "")
                sets.append(f"{k} = ?")
                params.append(v)
            if not sets:
                continue
            sets.append("updated_at = ?")
            params.extend([now, pid])
            cur = db.execute(
                f"UPDATE tariffs SET {', '.join(sets)} WHERE product_id = ?", params
            )
            saved += cur.rowcount or 0
        db.commit()
    invalidate_cache()
    return saved
