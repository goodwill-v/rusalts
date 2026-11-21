"""Секция «Как пользоваться» на страницах продуктов (задача Шефа 19.11):
команды ботов, что делает, что требуется от пользователя; поле
commands_html в товарах перекрывает стандартный гайд."""

from __future__ import annotations

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
    # products_store.DB_PATH прибинжен при первом импорте — изолируем,
    # иначе строки товаров из соседних тестов «переезжают» в этот (инцидент 19.11)
    from app import products_store
    monkeypatch.setattr(products_store, "DB_PATH", data / "shop.db")
    from app.main import app

    return TestClient(app)


@pytest.mark.parametrize("pid,marker", [
    ("vezhpom", "/monitor"),
    ("kupec", "/compare"),
    ("neyrosotrudnik", "GigaChat"),
    ("alt_expert", "/consultant"),
])
def test_commands_guide_renders(client, pid, marker) -> None:
    r = client.get(f"/techologis/p/{pid}/")
    assert r.status_code == 200
    assert "Как пользоваться" in r.text
    assert marker in r.text


def test_commands_html_override(client) -> None:
    """Поле товара commands_html перекрывает стандартный гайд."""
    r = client.post("/admin/api/products", headers={
        "Authorization": "Basic " + __import__("base64").b64encode(b"admin:20rusalt13").decode(),
    }, json={
        "id": "vezhpom", "name": "ВежПом", "description": "описание",
        "price_rub": 150, "status": "available", "commands_html": "МОЙ ГАЙД <b>ЖИРНЫЙ</b>",
    })
    assert r.status_code == 200, r.text
    html = client.get("/techologis/p/vezhpom/").text
    assert "МОЙ ГАЙД" in html and "<b>ЖИРНЫЙ</b>" in html
    assert "/monitor" not in html  # стандартный гайд перекрыт
