"""Реконсилиатор платежей (инцидент 09.11: webhook из ЮKassa не дошёл —
платёж оплачен, у нас pending). Логика: pending за 48 ч → get_payment →
succeeded → process_succeeded_payment (статус+карта+подписка)."""

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
    monkeypatch.setattr(config, "IS_YOOKASSA_SANDBOX", False)
    from app.main import app

    from app.tariffs_store import seed_defined_products
    seed_defined_products()
    return TestClient(app)


def _mk_pending(cid, yk):
    from app.payments import record_payment

    record_payment(yk, "kupec", 150, status="pending", customer_id=cid, plan="advanced")


def test_list_stale_pending_skips_sandbox(client) -> None:
    from app.payments import list_stale_pending, record_payment

    record_payment("sandbox-fake", "kupec", 150, status="pending", customer_id="1")
    _mk_pending("222", "2qwe-real-1")
    rows = list_stale_pending(hours=48)
    assert [r["yookassa_id"] for r in rows] == ["2qwe-real-1"]


def test_reconcile_activates_succeeded(client) -> None:
    import asyncio

    from app.routers.sandbox_shop import process_succeeded_payment

    _mk_pending("333", "2qwe-real-2")
    # ответ ЮKassa, каким его видит реконсилиатор
    real = {
        "status": "succeeded",
        "payment_method": {"id": "pm-222", "card": {"card_type": "mir", "last4": "1038"}},
    }
    # чистая функция активации — ровно то, что зовёт цикл реконсилиатора
    asyncio.run(process_succeeded_payment("2qwe-real-2", real))

    from app.payments import get_payment_record, latest_subscription_row

    rec = get_payment_record("2qwe-real-2")
    assert rec["status"] == "succeeded"
    assert rec["payment_method_id"] == "pm-222"
    assert rec["pm_last4"] == "1038"
    row = latest_subscription_row("333", "kupec")
    assert row["plan"] == "advanced"
    assert row["status"] == "active"


def _mk_get(real_map):
    async def fake_get_payment(pid):
        if pid in real_map:
            return real_map[pid]
        raise RuntimeError("not found")

    return fake_get_payment
