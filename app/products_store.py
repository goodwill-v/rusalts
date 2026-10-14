"""Store товаров витрины (CRUD админ-панели).

БД: data/shop.db, таблица products. Каталог = статические PRODUCTS из
app.payments, поверх которых накладываются записи из БД (переопределение
по id или добавление новых). Статусы БД: available/free/coming/inquiry/dev
(видны на витрине) и draft/archived (скрыты с витрины, видны в админке).
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

from app import config

DB_PATH = config.DATA_DIR / "shop.db"

# Статусы, показываемые на витрине
VISIBLE_STATUSES = {"available", "free", "coming", "inquiry", "beta", "dev"}
# Все допустимые статусы (draft/archived — только в админке)
ALL_STATUSES = VISIBLE_STATUSES | {"draft", "archived"}

_FIELDS = (
    "id", "name", "description", "price_rub", "period_days", "trial_days",
    "category", "platform", "bot_url", "detail_url", "icon", "status",
    "sort_order",
)


def _connect() -> sqlite3.Connection:
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute(
        """CREATE TABLE IF NOT EXISTS products (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            price_rub INTEGER NOT NULL DEFAULT 0,
            period_days INTEGER NOT NULL DEFAULT 30,
            trial_days INTEGER NOT NULL DEFAULT 0,
            category TEXT NOT NULL DEFAULT 'product',
            platform TEXT NOT NULL DEFAULT '',
            bot_url TEXT NOT NULL DEFAULT '',
            detail_url TEXT NOT NULL DEFAULT '',
            icon TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'draft',
            sort_order INTEGER NOT NULL DEFAULT 100,
            updated_at TEXT NOT NULL DEFAULT ''
        )"""
    )
    return db


def list_db_products() -> list[dict[str, Any]]:
    with closing(_connect()) as db:
        rows = db.execute(
            f"SELECT {', '.join(_FIELDS)} FROM products ORDER BY sort_order, id"
        ).fetchall()
    return [dict(r) for r in rows]


def get_db_product(pid: str) -> dict[str, Any] | None:
    with closing(_connect()) as db:
        row = db.execute(
            f"SELECT {', '.join(_FIELDS)} FROM products WHERE id = ?", (pid,)
        ).fetchone()
    return dict(row) if row else None


def upsert_product(data: dict[str, Any]) -> dict[str, Any]:
    """Создать/обновить товар. Возвращает сохранённую запись."""
    pid = str(data.get("id") or "").strip()
    name = str(data.get("name") or "").strip()
    if not pid or not name:
        raise ValueError("id и name обязательны")
    status = str(data.get("status") or "draft").strip()
    if status not in ALL_STATUSES:
        raise ValueError(f"недопустимый статус: {status}")
    category = str(data.get("category") or "product").strip()
    if category not in {"product", "service"}:
        category = "product"

    row = {
        "id": pid,
        "name": name,
        "description": str(data.get("description") or "").strip(),
        "price_rub": max(0, int(data.get("price_rub") or 0)),
        "period_days": max(0, int(data.get("period_days") or 0)),
        "trial_days": max(0, int(data.get("trial_days") or 0)),
        "category": category,
        "platform": str(data.get("platform") or "").strip(),
        "bot_url": str(data.get("bot_url") or "").strip(),
        "detail_url": str(data.get("detail_url") or "").strip(),
        "icon": str(data.get("icon") or "").strip(),
        "status": status,
        "sort_order": int(data.get("sort_order") or 100),
        "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    placeholders = ", ".join(_FIELDS)
    assigns = ", ".join(f"{f} = :{f}" for f in _FIELDS if f != "id")
    with closing(_connect()) as db:
        db.execute(
            f"INSERT INTO products ({placeholders}) "
            f"VALUES ({', '.join(':' + f for f in _FIELDS)}) "
            f"ON CONFLICT(id) DO UPDATE SET {assigns}",
            row,
        )
        db.commit()
    return row


def delete_product(pid: str) -> bool:
    with closing(_connect()) as db:
        cur = db.execute("DELETE FROM products WHERE id = ?", (pid,))
        db.commit()
    return cur.rowcount > 0


def catalog(*, include_hidden: bool = False) -> list[dict[str, Any]]:
    """Каталог витрины: статические PRODUCTS + оверрайды из БД.

    include_hidden=True — для админки (draft/archived тоже).
    """
    from app.payments import PRODUCTS

    items: dict[str, dict[str, Any]] = {}
    for pid, p in PRODUCTS.items():
        if pid == "italidia_test":
            continue
        item = dict(p)
        item.setdefault("detail_url", "")
        item.setdefault("icon", "")
        items[pid] = item

    for row in list_db_products():
        existing = items.get(row["id"])
        if existing:
            for f in _FIELDS:
                if f == "id":
                    continue
                # пустые значения в БД не затирают статические
                if row[f] not in (None, "") or f in ("status", "sort_order"):
                    existing[f] = row[f]
        else:
            items[row["id"]] = dict(row)

    result = list(items.values())
    if not include_hidden:
        result = [p for p in result if p.get("status") in VISIBLE_STATUSES]
    return result
