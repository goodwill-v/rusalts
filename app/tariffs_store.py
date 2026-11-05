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


# ─── Тарифные планы (ТЗ Vitrina tarify v2, generalized) ──────────────────
# plans: покупаемые пакеты продукта (ВежПом: monitoring/guard/business).
# Базовый тариф (tariffs) остаётся для витринной карточки и ботов до перехода.

PLAN_FIELDS = (
    "name", "price_rub", "period_days", "trial_days",
    "resource_limit", "searches_per_month", "position", "active",
)


def _plans_connect() -> sqlite3.Connection:
    db = sqlite3.connect(str(config.DATA_DIR / "shop.db"))
    db.row_factory = sqlite3.Row
    db.execute(
        """CREATE TABLE IF NOT EXISTS tariff_plans (
            product_id TEXT NOT NULL,
            plan TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            price_rub INTEGER NOT NULL DEFAULT 0,
            period_days INTEGER NOT NULL DEFAULT 30,
            trial_days INTEGER NOT NULL DEFAULT 0,
            resource_limit INTEGER NOT NULL DEFAULT 0,
            searches_per_month INTEGER NOT NULL DEFAULT 0,
            position INTEGER NOT NULL DEFAULT 100,
            active INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (product_id, plan)
        )"""
    )
    # Идемпотентная миграция для существующих таблиц (поиски/мес Купца)
    cols = {r[1] for r in db.execute("PRAGMA table_info(tariff_plans)").fetchall()}
    if "searches_per_month" not in cols:
        db.execute(
            "ALTER TABLE tariff_plans ADD COLUMN searches_per_month INTEGER NOT NULL DEFAULT 0"
        )
        db.commit()
    return db


def seed_product_plans(product_id: str, plans: list[dict[str, Any]]) -> int:
    """Идемпотентный сидинг планов продукта (только если у продукта ещё нет планов)."""
    n = 0
    with closing(_plans_connect()) as db:
        have = db.execute(
            "SELECT COUNT(*) FROM tariff_plans WHERE product_id = ?", (product_id,)
        ).fetchone()[0]
        if have:
            return 0
        for p in plans:
            db.execute(
                "INSERT INTO tariff_plans (product_id, plan, name, price_rub, period_days,"
                " trial_days, resource_limit, searches_per_month, position, active)"
                " VALUES (?,?,?,?,?,?,?,?,?,1)",
                (
                    product_id, p.get("plan", ""), p.get("name", ""),
                    p.get("price_rub", 0), p.get("period_days", 30),
                    p.get("trial_days", 0), p.get("resource_limit", 0),
                    p.get("searches_per_month", 0), p.get("position", 100),
                ),
            )
            n += 1
        db.commit()
    return n


def list_plans(product_id: str, *, active_only: bool = True) -> list[dict[str, Any]]:
    q = "SELECT * FROM tariff_plans WHERE product_id = ?"
    if active_only:
        q += " AND active = 1"
    q += " ORDER BY position, price_rub"
    with closing(_plans_connect()) as db:
        rows = db.execute(q, (product_id,)).fetchall()
    return [dict(r) for r in rows]


def upsert_plan(data: dict[str, Any]) -> dict[str, Any]:
    pid = str(data.get("product_id") or "").strip()
    plan = str(data.get("plan") or "").strip().lower()
    if not pid or not plan:
        raise ValueError("product_id и plan обязательны")
    fields = {
        "name": str(data.get("name") or "").strip()[:80],
        "price_rub": max(0, int(data.get("price_rub") or 0)),
        "period_days": max(1, int(data.get("period_days") or 30)),
        "trial_days": max(0, int(data.get("trial_days") or 0)),
        "resource_limit": max(0, int(data.get("resource_limit") or 0)),
        "searches_per_month": max(0, int(data.get("searches_per_month") or 0)),
        "position": int(data.get("position") or 100),
        "active": 1 if data.get("active") in (1, "1", True) else 0,
    }
    sets = ", ".join(f"{k} = ?" for k in fields)
    with closing(_plans_connect()) as db:
        exists = db.execute(
            "SELECT 1 FROM tariff_plans WHERE product_id = ? AND plan = ?", (pid, plan)
        ).fetchone()
        if exists:
            db.execute(
                f"UPDATE tariff_plans SET {sets} WHERE product_id = ? AND plan = ?",
                (*fields.values(), pid, plan),
            )
        else:
            cols = ["product_id", "plan", *fields]
            db.execute(
                f"INSERT INTO tariff_plans ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                (pid, plan, *fields.values()),
            )
        db.commit()
        row = db.execute(
            "SELECT * FROM tariff_plans WHERE product_id = ? AND plan = ?", (pid, plan)
        ).fetchone()
    invalidate_cache()
    return dict(row)


def delete_plan(product_id: str, plan: str) -> bool:
    with closing(_plans_connect()) as db:
        cur = db.execute(
            "DELETE FROM tariff_plans WHERE product_id = ? AND plan = ?",
            (product_id, plan),
        )
        db.commit()
    invalidate_cache()
    return cur.rowcount > 0


# ─── Сидинг планов продуктов (ТЗ Купец v2 §7, решение Шефа 05.11) ─────────
# Заполняется на старте приложения, только если у продукта ещё нет планов
# (правки из админки никогда не перезаписываются).
PRODUCT_PLAN_SEEDS: dict[str, list[dict[str, Any]]] = {
    "kupec": [
        {
            "plan": "base", "name": "Базовый", "price_rub": 150,
            "period_days": 30, "trial_days": 7,
            "resource_limit": 3,          # слежки «Жду скидку»
            "searches_per_month": 80,     # корректировка Шефа (PR #79): месячные пакеты
            "position": 10,
        },
        {
            "plan": "advanced", "name": "Продвинутый", "price_rub": 500,
            "period_days": 30, "trial_days": 7,
            "resource_limit": 10, "searches_per_month": 270, "position": 20,
        },
        {
            "plan": "marketing", "name": "Маркетинг", "price_rub": 1500,
            "period_days": 30, "trial_days": 7,
            "resource_limit": 30, "searches_per_month": 800, "position": 30,
        },
    ],
    # ВежПом (ТЗ Vitrina tarify v2): monitoring/guard/business — общий пул
    # ресурсы (сайты/серверы + домены). В проде планы уже есть — сид молчит.
    "vezhpom": [
        {
            "plan": "monitoring", "name": "Мониторинг", "price_rub": 150,
            "period_days": 30, "trial_days": 7, "resource_limit": 3,
            "searches_per_month": 0, "position": 10,
        },
        {
            "plan": "guard", "name": "Страж", "price_rub": 500,
            "period_days": 30, "trial_days": 7, "resource_limit": 10,
            "searches_per_month": 0, "position": 20,
        },
        {
            "plan": "business", "name": "Бизнес", "price_rub": 1500,
            "period_days": 30, "trial_days": 7, "resource_limit": 30,
            "searches_per_month": 0, "position": 30,
        },
    ],
}


def seed_defined_products() -> int:
    """Идемпотентный сидинг планов по PRODUCT_PLAN_SEEDS (вызов на старте)."""
    n = 0
    for pid, plans in PRODUCT_PLAN_SEEDS.items():
        n += seed_product_plans(pid, plans)
    return n
