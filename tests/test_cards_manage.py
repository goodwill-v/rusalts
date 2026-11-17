"""Отвязка карты на сайте (требование ЮKassa, решение Шефа 09.11):
подписанная ссылка из бота → скрытый блок на витрине → список карт → отвязка."""

from __future__ import annotations

import sys
from base64 import b64encode
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config


@pytest.fixture()
def client(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "VITRINA_API_KEYS", ["test-key"])
    from app.main import app

    return TestClient(app)


AUTH = {"Authorization": "Basic " + b64encode(b"admin:20rusalt13").decode()}


def _add_paid_pm(client, cid, pid="vezhpom", yk="sbx-1", last4="4242"):
    """Платёж с привязанной картой — через локальную БД (вебхук-путь)."""
    import sqlite3

    from app.payments import record_payment

    record_payment(yk, pid, 150, status="pending", customer_id=cid)
    db = sqlite3.connect(str(config.DATA_DIR / "payments.db"))
    db.execute(
        "UPDATE payments SET status='succeeded', payment_method_id=?, pm_brand=?, pm_last4=?"
        " WHERE yookassa_id=?",
        (f"pm-{yk}", "mastercard", last4, yk),
    )
    db.commit()
    db.close()


def test_cards_link_requires_api_key(client) -> None:
    r = client.get("/api/cards-link", params={"customer_id": "123"})
    assert r.status_code == 401
    r2 = client.get("/api/cards-link", params={"customer_id": "123"},
                    headers={"X-API-Key": "wrong"})
    assert r2.status_code == 401


def test_link_and_list_and_unlink(client, monkeypatch) -> None:
    from app import config as cfg

    cid = "433677408"
    _add_paid_pm(client, cid)

    # боты получают ссылку по X-API-Key
    key = (cfg.VITRINA_API_KEYS or ["test-key"])[0]
    r = client.get("/api/cards-link", params={"customer_id": cid},
                   headers={"X-API-Key": key})
    assert r.status_code == 200
    url = r.json()["url"]
    assert "/techologis/?cards=" in url and "sig=" in url

    # параметры из URL
    from urllib.parse import parse_qs, urlparse

    q = parse_qs(urlparse(url).query)
    cid_q, exp, sig = q["cards"][0], q["exp"][0], q["sig"][0]

    # список карт по подписи
    r2 = client.get(f"/api/payment-methods/{cid}", params={"exp": exp, "sig": sig})
    assert r2.status_code == 200
    cards = r2.json()["cards"]
    assert len(cards) == 1
    assert cards[0]["label"].endswith("••4242")
    assert cards[0]["product_id"] == "vezhpom"

    # без подписи — 403
    assert client.get(f"/api/payment-methods/{cid}").status_code == 403
    # просроченная подпись — 403
    assert client.get(f"/api/payment-methods/{cid}",
                      params={"exp": "1000000000", "sig": sig}).status_code == 403

    # отвязка с той же подписью
    monkeypatch.setattr(cfg, "IS_YOOKASSA_SANDBOX", True)  # не дёргаем ЮKassa
    r3 = client.post("/api/payment-method/delete", json={
        "customer_id": cid, "product_id": "vezhpom", "exp": exp, "sig": sig,
    })
    assert r3.status_code == 200 and r3.json()["removed"] is True

    # после отвязки список пуст
    r4 = client.get(f"/api/payment-methods/{cid}", params={"exp": exp, "sig": sig})
    assert r4.json()["cards"] == []

    # отвязка без auth — 403
    r5 = client.post("/api/payment-method/delete", json={"customer_id": cid, "product_id": "vezhpom"})
    assert r5.status_code == 403


def test_showcase_page_renders_with_cards_params(client) -> None:
    r = client.get("/techologis/", params={"cards": "1", "exp": "9999999999", "sig": "x"})
    assert r.status_code == 200
    assert "cards-manage" in r.text
