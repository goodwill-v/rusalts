"""E4 ТЗ Развитие: регресс-тест согласованности платформы.

Витрина (catalog/страницы), тарифы (shop.db), тарифные планы и API должны
говорить «одним голосом»: цена витринной карточки = тарифу в БД, планы в
API = строкам в БД, «Скоро»-продукты не продаются, ключевые страницы живы.
Расхождение в любом месте — красный CI.
"""

from __future__ import annotations

import re
import sys
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
    from app.main import app

    from app.tariffs_store import seed_defined_products
    seed_defined_products()
    return TestClient(app)


def _visible_products(client):
    from app.products_store import catalog

    return catalog()


def test_showcase_price_matches_tariff_db(client) -> None:
    """Цена витринной карточки (catalog) = цене тарифа в shop.db (Этап A)."""
    from app.tariffs_store import list_tariffs

    db = {t["product_id"]: t for t in list_tariffs()}
    for p in _visible_products(client):
        t = db.get(p["id"])
        if t and p.get("price_rub"):
            assert p["price_rub"] == int(t["price_rub"]), (
                f"{p['id']}: витрина {p['price_rub']}₽ ≠ тариф БД {t['price_rub']}₽"
            )


def test_plans_api_matches_db(client) -> None:
    """GET /api/tariffs/{id} отдаёт ровно то, что в tariff_plans."""
    from app.tariffs_store import list_plans

    for pid in ("kupec", "vezhpom"):
        api_plans = client.get(f"/api/tariffs/{pid}").json()["plans"]
        db_plans = list_plans(pid)
        assert [(p["plan"], p["price_rub"], p["searches_per_month"]) for p in api_plans] == [
            (p["plan"], p["price_rub"], p["searches_per_month"]) for p in db_plans
        ]


def test_product_page_renders_db_plan_prices(client) -> None:
    """Карточки планов на странице продукта = ценам из БД (без расхождений)."""
    from app.tariffs_store import list_plans

    html = client.get("/techologis/p/kupec/").text
    for pl in list_plans("kupec"):
        assert f"{pl['price_rub']} ₽" in html, f"план {pl['plan']}: цены нет на странице"
        if pl["searches_per_month"]:
            assert f"Поисков: {pl['searches_per_month']}/мес" in html


def test_not_available_products_not_buyable(client) -> None:
    """«Скоро»/черновики не попадают в покупаемое (E2-инвариант)."""
    for p in _visible_products(client):
        if p.get("status") != "available":
            for r in client.get("/techologis/").text.split("btn-buy")[1:]:
                pass  # страница не должна содержать кнопки покупки для недоступных
        # buyable-фильтр считается на сервере: недоступный не в buyable списках
    html = client.get("/techologis/").text
    for p in _visible_products(client):
        if p.get("status") not in ("available",):
            continue
    # прямо: data-product кнопки ссылаются только на доступные с ценой
    buyable_ids = {p["id"] for p in _visible_products(client)
                   if p.get("status") == "available" and p.get("price_rub", 0) > 0}
    for pid in re.findall(r'data-product="([^"]+)"', html):
        assert pid in buyable_ids, f"кнопка покупки у недоступного продукта: {pid}"


def test_key_pages_live(client) -> None:
    """Ключевые точки входа отвечают 200 (мини-версия ночного чека E2 в CI)."""
    for path in (
        "/", "/techologis/", "/news/", "/consultant", "/diagnostics/",
        "/legal/privacy/", "/techologis/p/kupec/",
    ):
        r = client.get(path)
        assert r.status_code == 200, f"{path} → {r.status_code}"


def test_admin_funnel_endpoint(client) -> None:
    """G3: воронка в админке — считаем по своим БД без внешних API."""
    from base64 import b64encode

    auth = {"Authorization": "Basic " + b64encode(b"admin:20rusalt13").decode()}
    r = client.get("/admin/api/funnel", headers=auth)
    assert r.status_code == 200
    f = r.json()
    for k in ("diagnostics_30d", "leads_30d", "payments_30d", "revenue_30d",
              "revenue_total", "active_subscriptions", "lead_rate_30d", "conversion_30d"):
        assert k in f, f"нет поля {k}"
    assert f["payments_30d"] == 0  # чистая БД — нули, но структура полная


def test_subscription_status_page(client) -> None:
    """Страница подписки: 200 во всех вариантах, статус+план, без 500
    (баг 06.11: старый роут рендерил витринный шаблон без контекста)."""
    # без параметров — подсказка, 200
    r = client.get("/techologis/subscription/")
    assert r.status_code == 200
    # несуществующий клиент — сообщение об ошибке, не 500
    r2 = client.get("/techologis/subscription/?customer_id=1&product_id=vezhpom")
    assert r2.status_code == 200
    assert "не найдена" in r2.text
    # существующий: грант доступа → страница показывает план «Бизнес» и ссылку на планы
    from base64 import b64encode
    auth = {"Authorization": "Basic " + b64encode(b"admin:20rusalt13").decode()}
    client.post("/api/billing/grant-access",
                json={"customer_id": "62141768", "product_id": "vezhpom", "reason": "тест"},
                headers=auth)
    r3 = client.get("/techologis/subscription/?customer_id=62141768&product_id=vezhpom")
    assert r3.status_code == 200
    assert "Бизнес" in r3.text
    assert "/techologis/p/vezhpom/" in r3.text
