"""Тарифные планы Купца на витрине (ТЗ Купец v2 §7, запрос Купа через Кора).

Покрывает: поле searches_per_month (миграция+CRUD), сидинг kupec-планов,
флаг триала в /api/subscription, старт триала, запись плана вебхуком.
"""

from __future__ import annotations

import sys
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Приложение с изолированной data-папкой (свои shop.db/payments.db/billing.db)."""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", data)
    from app.main import app

    # Сид на старте не сработал (app уже импортирован) — зовём напрямую
    from app.tariffs_store import seed_defined_products
    seed_defined_products()
    return TestClient(app)


# ─── Сидинг и миграция ────────────────────────────────────────────────────


def test_seed_kupec_plans(client) -> None:
    from app.tariffs_store import list_plans

    plans = {p["plan"]: p for p in list_plans("kupec")}
    assert set(plans) == {"base", "advanced", "marketing"}
    assert plans["base"]["price_rub"] == 150
    assert plans["advanced"]["price_rub"] == 500
    assert plans["marketing"]["price_rub"] == 1500
    # resource_limit = слежки 3/10/30 (решение Шефа №1)
    assert [plans[k]["resource_limit"] for k in ("base", "advanced", "marketing")] == [3, 10, 30]
    # поиски/мес 80/270/800 (корректировка Шефа, PR #79)
    assert [plans[k]["searches_per_month"] for k in ("base", "advanced", "marketing")] == [80, 270, 800]
    # триал 7 дней у всех
    assert all(p["trial_days"] == 7 for p in plans.values())


def test_seed_idempotent_keeps_admin_edits(client) -> None:
    from app.tariffs_store import seed_defined_products, upsert_plan

    upsert_plan({"product_id": "kupec", "plan": "base", "name": "Базовый",
                 "price_rub": 999, "period_days": 30, "trial_days": 7,
                 "resource_limit": 3, "searches_per_month": 80, "active": 1})
    assert seed_defined_products() == 0  # планы есть — сид молчит
    from app.tariffs_store import list_plans
    base = next(p for p in list_plans("kupec") if p["plan"] == "base")
    assert base["price_rub"] == 999  # правку админки не перезаписали


def test_migration_adds_searches_column(tmp_path, monkeypatch) -> None:
    """Старая таблица без searches_per_month получает колонку (идемпотентно)."""
    data = tmp_path / "data_old"
    data.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", data)
    db = sqlite3.connect(str(data / "shop.db"))
    db.execute(
        """CREATE TABLE tariff_plans (
            product_id TEXT NOT NULL, plan TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '', price_rub INTEGER NOT NULL DEFAULT 0,
            period_days INTEGER NOT NULL DEFAULT 30, trial_days INTEGER NOT NULL DEFAULT 0,
            resource_limit INTEGER NOT NULL DEFAULT 0,
            position INTEGER NOT NULL DEFAULT 100, active INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (product_id, plan))"""
    )
    db.commit()
    db.close()

    from app.tariffs_store import _plans_connect, upsert_plan
    with _plans_connect() as db:
        cols = {r[1] for r in db.execute("PRAGMA table_info(tariff_plans)").fetchall()}
    assert "searches_per_month" in cols
    upsert_plan({"product_id": "x", "plan": "p1", "searches_per_month": 42})
    with _plans_connect() as db:
        v = db.execute(
            "SELECT searches_per_month FROM tariff_plans WHERE product_id='x' AND plan='p1'"
        ).fetchone()[0]
    assert v == 42


# ─── API витрины ──────────────────────────────────────────────────────────


def test_api_tariffs_include_searches(client) -> None:
    r = client.get("/api/tariffs/kupec")
    assert r.status_code == 200
    plans = {p["plan"]: p for p in r.json()["plans"]}
    assert plans["marketing"]["searches_per_month"] == 800
    assert plans["base"]["searches_per_month"] == 80


def test_trial_start_and_subscription_flag(client) -> None:
    cid = "12345"
    # 1) старт триала на плане marketing
    r = client.post("/api/subscription/trial/start",
                    json={"customer_id": cid, "product_id": "kupec", "plan": "marketing"})
    assert r.status_code == 200
    data = r.json()
    assert data["trial"] is True
    assert data["plan"] == "marketing"
    assert data["trial_days"] == 7

    # 2) повторный старт отклонён (один триал навсегда)
    r2 = client.post("/api/subscription/trial/start",
                     json={"customer_id": cid, "product_id": "kupec", "plan": "marketing"})
    assert r2.json()["trial"] is False
    assert r2.json()["reason"] == "trial_used"

    # 3) GET /api/subscription отдаёт trial=true без токен-баланса (404 → 200)
    r3 = client.get(f"/api/subscription/{cid}/kupec")
    assert r3.status_code == 200
    sub = r3.json()
    assert sub["trial"] is True
    assert sub["status"] == "trial"
    assert sub["searches_per_month"] == 800

    # 4) кто без подписки и без триала — по-прежнему 404
    r4 = client.get("/api/subscription/99999/kupec")
    assert r4.status_code == 404


def test_trial_default_plan_is_first(client) -> None:
    r = client.post("/api/subscription/trial/start",
                    json={"customer_id": "777", "product_id": "kupec"})
    assert r.status_code == 200
    assert r.json()["plan"] == "base"  # первый по position


def test_webhook_persists_plan(client, monkeypatch) -> None:
    """Вебхук пишет план в subscriptions — иначе /api/subscription не видит план
    и гейтинг бота фолбэчит всех платных на первый план."""
    import app.routers.sandbox_shop as shop
    from app.payments import record_payment, latest_subscription_row

    yk_id = "test-webhook-plan-1"
    record_payment(yk_id, "kupec", 500, status="succeeded",
                   customer_email="", customer_id="555000111", plan="advanced")

    async def fake_get_payment(pid):  # ЮKassa не дёргаем
        return {"status": "succeeded", "id": pid}

    monkeypatch.setattr(shop, "get_payment", fake_get_payment)
    r = client.post("/api/payment-webhook",
                    json={"event": "payment.succeeded", "object": {"id": yk_id}})
    assert r.status_code == 200

    row = latest_subscription_row("555000111", "kupec")
    assert row is not None
    assert row["status"] == "active"
    assert row["plan"] == "advanced"

    # и /api/subscription теперь показывает план + пакет поисков
    sub = client.get("/api/subscription/555000111/kupec").json()
    assert sub["plan"] == "advanced"
    assert sub["searches_per_month"] == 270
    assert sub["trial"] is False
