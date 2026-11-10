"""Контент-блоки админки как HTML (решение Шефа 10.11): теги и
переносы Enter работают на страницах сайта."""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config

AUTH = {"Authorization": "Basic " + base64.b64encode(b"admin:20rusalt13").decode()}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", data)
    from app.main import app

    return TestClient(app)


def test_content_block_renders_html_and_newlines(client) -> None:
    # сохраняем блок через тот же API, что и админка («📝 Контент»);
    # \n в значении = Enter в поле админки
    r = client.post(
        "/admin/api/content-blocks",
        headers=AUTH,
        json={
            "page": "showcase",
            "blocks": {
                "lead": "Первый абзац с <b>выделением</b>.\nВторая строка после Enter."
            },
        },
    )
    assert r.status_code == 200, r.text

    html = client.get("/techologis/").text
    assert "<b>выделением</b>" in html            # тег отрендерен, не экранирован
    assert "&lt;b&gt;" not in html                # и не показан как текст
    assert "Вторая строка после Enter" in html


def test_content_block_default_without_row(client) -> None:
    # нет записи — работает дефолт (страница не падает)
    html = client.get("/techologis/").text
    assert "Продукты и услуги АЛТ" in html
