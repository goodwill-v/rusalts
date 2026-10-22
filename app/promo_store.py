"""Промокоды (Этап C4): скидки при оплате с серверным пересчётом цены.

shop.db, таблица promo_codes. Тип скидки: percent (1–99) или fixed (₽).
Учёт применений — таблица promo_uses + счётчик used_count. Валидация —
только на сервере (клиент шлёт код, цену считает бэкенд).
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

from app import config


def _connect() -> sqlite3.Connection:
    db = sqlite3.connect(str(config.DATA_DIR / "shop.db"))
    db.row_factory = sqlite3.Row
    db.execute(
        """CREATE TABLE IF NOT EXISTS promo_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            discount_type TEXT NOT NULL DEFAULT 'percent',
            discount_value INTEGER NOT NULL DEFAULT 0,
            product_id TEXT NOT NULL DEFAULT '',
            valid_until TEXT NOT NULL DEFAULT '',
            max_uses INTEGER NOT NULL DEFAULT 0,
            used_count INTEGER NOT NULL DEFAULT 0,
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT ''
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS promo_uses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            product_id TEXT NOT NULL,
            customer_id TEXT NOT NULL DEFAULT '',
            original_rub INTEGER NOT NULL,
            discounted_rub INTEGER NOT NULL,
            used_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    return db


def normalize_code(code: str) -> str:
    return (code or "").strip().upper()[:40]


def validate(code: str, product_id: str, price_rub: int) -> dict[str, Any] | None:
    """Вернуть {'discount_rub','final_rub','code',...} или {'error': ...} / None."""
    code = normalize_code(code)
    if not code:
        return None
    with closing(_connect()) as db:
        row = db.execute(
            "SELECT * FROM promo_codes WHERE code = ?", (code,)
        ).fetchone()
    if not row:
        return {"error": "Промокод не найден"}
    if row["product_id"] and row["product_id"] != product_id:
        return {"error": "Промокод не действует на этот продукт"}
    if row["valid_until"]:
        try:
            until = datetime.fromisoformat(row["valid_until"])
            if datetime.now(timezone.utc) > until:
                return {"error": "Срок действия промокода истёк"}
        except ValueError:
            pass
    if row["max_uses"] and row["used_count"] >= row["max_uses"]:
        return {"error": "Лимит применений промокода исчерпан"}

    price = max(0, int(price_rub or 0))
    if row["discount_type"] == "percent":
        discount = round(price * min(99, max(1, row["discount_value"])) / 100)
    else:
        discount = min(price, int(row["discount_value"]))
    final = max(0, price - discount)
    if final >= price:
        return {"error": "Скидка не применяется к этой цене"}
    return {
        "code": code,
        "discount_rub": discount,
        "final_rub": final,
        "original_rub": price,
    }


def record_use(code: str, product_id: str, customer_id: str,
               original_rub: int, discounted_rub: int) -> None:
    code = normalize_code(code)
    with closing(_connect()) as db:
        db.execute(
            "INSERT INTO promo_uses (code, product_id, customer_id, original_rub, discounted_rub)"
            " VALUES (?,?,?,?,?)",
            (code, product_id, customer_id, original_rub, discounted_rub),
        )
        db.execute(
            "UPDATE promo_codes SET used_count = used_count + 1 WHERE code = ?", (code,)
        )
        db.commit()


def list_promos() -> list[dict[str, Any]]:
    with closing(_connect()) as db:
        rows = db.execute("SELECT * FROM promo_codes ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


def upsert_promo(data: dict[str, Any]) -> dict[str, Any]:
    code = normalize_code(data.get("code") or "")
    if not code:
        raise ValueError("код обязателен")
    dtype = data.get("discount_type") if data.get("discount_type") in ("percent", "fixed") else "percent"
    fields = {
        "code": code,
        "discount_type": dtype,
        "discount_value": max(0, int(data.get("discount_value") or 0)),
        "product_id": str(data.get("product_id") or "").strip(),
        "valid_until": str(data.get("valid_until") or "").strip(),
        "max_uses": max(0, int(data.get("max_uses") or 0)),
        "note": str(data.get("note") or "").strip()[:200],
    }
    pid = int(data.get("id") or 0)
    with closing(_connect()) as db:
        if pid:
            db.execute(
                "UPDATE promo_codes SET code=:code, discount_type=:discount_type,"
                " discount_value=:discount_value, product_id=:product_id,"
                " valid_until=:valid_until, max_uses=:max_uses, note=:note"
                " WHERE id=:id",
                {**fields, "id": pid},
            )
        else:
            fields["created_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            cols = ", ".join(fields)
            db.execute(
                f"INSERT INTO promo_codes ({cols}) VALUES ({', '.join(':' + k for k in fields)})",
                fields,
            )
        db.commit()
        row = db.execute(
            "SELECT * FROM promo_codes WHERE code = ?", (code,)
        ).fetchone()
    return dict(row)


def delete_promo(pid: int) -> bool:
    with closing(_connect()) as db:
        cur = db.execute("DELETE FROM promo_codes WHERE id = ?", (pid,))
        db.commit()
    return cur.rowcount > 0
