"""Финансовый блок админки (ТЗ Алика 10.11): overview-дашборд, mark-refund,
audit log, подписки в блоке карт витрины."""

from __future__ import annotations

import sys
import sqlite3
from base64 import b64encode
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config

AUTH = {"Authorization": "Basic " + b64encode(b"admin:20rusalt13").decode()}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "VITRINA_API_KEYS", ["test-key"])
    monkeypatch.setattr(config, "IS_YOOKASSA_SANDBOX", False)
    from app.main import app

    from app.tariffs_store import seed_defined_products
    seed_defined_products()
    return TestClient(app)


def _mk_paid(client, cid, yk, pm="pm-1", last4="1234", plan=""):
    from app.payments import record_payment, activate_subscription
    record_payment(yk, "vezhpom", 150, status="pending", customer_id=cid, plan=plan)
    db = sqlite3.connect(str(config.DATA_DIR / "payments.db"))
    db.execute(
        "UPDATE payments SET status='succeeded', payment_method_id=?, pm_brand=?, pm_last4=?"
        " WHERE yookassa_id=?", (pm, "mastercard", last4, yk),
    )
    db.commit()
    db.close()
    activate_subscription(0, "vezhpom", "", pm, period_days=30, plan=plan, customer_id=cid)


def test_finance_overview(client) -> None:
    _mk_paid(client, "tg_1", "yk-fin-1")
    r = client.get("/admin/api/finance/overview", headers=AUTH)
    assert r.status_code == 200
    d = r.json()
    assert d["stats"]["total_revenue_month"] == 150
    assert d["stats"]["active_subscriptions"] >= 1
    assert d["stats"]["month_plan"] > 0
    sub = d["subscriptions"][0]
    assert sub["card_label"].endswith("••1234")
    assert sub["payment_id"] == "yk-fin-1"
    assert sub["auto_renew"] is True
    assert d["by_product"][0]["product_id"] == "vezhpom"


def test_mark_refund_and_audit(client) -> None:
    _mk_paid(client, "tg_2", "yk-fin-2")
    r = client.post("/admin/api/finance/mark-refund",
                    json={"payment_id": "yk-fin-2", "note": "тестовый возврат"}, headers=AUTH)
    assert r.status_code == 200
    # статус изменился
    import sqlite3
    db = sqlite3.connect(str(config.DATA_DIR / "payments.db"))
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT status FROM payments WHERE yookassa_id='yk-fin-2'").fetchone()
    assert row["status"] == "refunded"
    # журнал содержит обе записи
    d = client.get("/admin/api/finance/overview", headers=AUTH).json()
    actions = [a["action"] for a in d["audit_log"]]
    assert "refund_marked" in actions


def test_unbind_logged(client, monkeypatch) -> None:
    monkeypatch.setattr(config, "IS_YOOKASSA_SANDBOX", True)  # не дёргаем ЮKassa с фейковой картой
    _mk_paid(client, "tg_3", "yk-fin-3")
    key = {"X-API-Key": "test-key"}
    r = client.post("/api/payment-method/delete",
                    json={"customer_id": "tg_3", "product_id": "vezhpom"}, headers=key)
    assert r.status_code == 200 and r.json()["removed"] is True
    d = client.get("/admin/api/finance/overview", headers=AUTH).json()
    actions = [a["action"] for a in d["audit_log"]]
    assert "unbind_card" in actions
    det = next(a for a in d["audit_log"] if a["action"] == "unbind_card")
    assert "****1234" in det["detail"]


def test_grant_and_revoke_logged(client) -> None:
    client.post("/api/billing/grant-access",
                json={"customer_id": "tg_4", "product_id": "kupec", "reason": "тест"}, headers=AUTH)
    r = client.post("/api/billing/revoke", json={"customer_id": "tg_4", "product_id": "kupec"},
                    headers=AUTH)
    assert r.status_code == 200
    d = client.get("/admin/api/finance/overview", headers=AUTH).json()
    actions = [a["action"] for a in d["audit_log"]]
    assert "revoke" in actions


def test_payment_methods_include_subscriptions(client) -> None:
    _mk_paid(client, "tg_5", "yk-fin-5")
    # подписанная ссылка
    key = {"X-API-Key": "test-key"}
    url = client.get("/api/cards-link", params={"customer_id": "tg_5"}, headers=key).json()["url"]
    from urllib.parse import parse_qs, urlparse

    q = parse_qs(urlparse(url).query)
    d = client.get("/api/payment-methods/tg_5",
                   params={"exp": q["exp"][0], "sig": q["sig"][0]}).json()
    assert d["cards"][0]["label"].endswith("••1234")
    subs = d["subscriptions"]
    assert subs and subs[0]["product_name"]  # имя продукта из каталога
    assert subs[0]["status"] == "active"
