"""Баг 10.11: промокод с valid_until без часового пояса (как сохраняет
админка) падал TypeError при сравнении с aware datetime → 500 вместо JSON.
Проверка: validate работает и не истёк."""

from __future__ import annotations

import sys
import sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config


def test_validate_accepts_naive_valid_until(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    from app import promo_store

    with promo_store._connect() as db:
        db.execute(
            """INSERT INTO promo_codes (code, product_id, discount_type, discount_value,
                                        valid_until, max_uses, used_count)
               VALUES ('START2026', 'neyrosotrudnik', 'percent', 10,
                       '2027-01-31', 0, 0)"""
        )
        db.commit()

    res = promo_store.validate("start2026", "neyrosotrudnik", price_rub=500)
    assert "error" not in res, res
    assert res["final_rub"] == 450  # −10 % от 500


def test_validate_expired_still_rejects(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    from app import promo_store

    with promo_store._connect() as db:
        db.execute(
            """INSERT INTO promo_codes (code, product_id, discount_type, discount_value,
                                        valid_until, max_uses, used_count)
               VALUES ('OLD', 'kupec', 'percent', 10, '2020-01-01', 0, 0)"""
        )
        db.commit()

    res = promo_store.validate("OLD", "kupec", price_rub=150)
    assert res.get("error") == "Срок действия промокода истёк"
