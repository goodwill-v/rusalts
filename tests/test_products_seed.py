"""Сид каталога для новых БД (хвост блока «оплата/тарифы», Кор 15.11):
свежая БД получает актуальный контент НейроСотрудника (правка Шефа 09.11)
и триал по старшему плану."""

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
    from app.main import app

    return TestClient(app)


def test_fresh_db_seed_has_neyrosotrudnik_content(client) -> None:
    """Новая БД: НейроСотрудник доступен, с HTML-описанием 09.11 и триалом 7."""
    from app.products_store import catalog

    p = next(x for x in catalog(include_hidden=True) if x["id"] == "neyrosotrudnik")
    assert p["status"] == "available"
    assert p["trial_days"] == 7
    assert "НейроСотрудник</b>" in p["description"]          # HTML-описание 09.11
    assert "GigaChat" in p["description_short"]              # баннер
    assert p["bot_url"] == "https://max.ru/id667207863875_bot"


def test_fresh_db_detail_page_renders_html(client) -> None:
    r = client.get("/techologis/p/neyrosotrudnik/")
    assert r.status_code == 200
    assert "<b>НейроСотрудник</b>" in r.text                 # HTML не экранирован
    assert "🎁 7 дней Пробный период" in r.text
