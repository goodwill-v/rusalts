"""«Свежие публикации» на /channels/ + редактор в админке (ТЗ МАКа 21.11):
сущность публикация (заголовок ≤100, закреп ≤3 с валидацией, черновики
скрыты), лента 10 (закреп сверху), анонс = первые 3 строки, admin CRUD."""

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
    # publications_store.DB_PATH прибинжен при первом импорте — изолируем,
    # иначе строки из соседних тестов «переезжают» (урок products_store)
    from app import publications_store
    monkeypatch.setattr(publications_store, "DB_PATH", data / "publications.db")
    from app.main import app

    return TestClient(app)


def _mk(client, title="Статья", pinned=False, visible=True, body=None, author="МАК"):
    from app.publications_store import save

    return save({
        "title": title,
        "body_html": body or "Первая строка анонса.\nВторая строка.\nТретья.\nЧетвёртая строка — в анонс не попадёт.",
        "image_url": "", "author": author,
        "published_at": "2026-11-21", "pinned": pinned, "visible": visible,
    })


def test_feed_returns_last10_pinned_first(client) -> None:
    # 12 видимых + 2 черновика + 3 «вне лимита» закрепа не создаём (макс 3)
    for i in range(12):
        _mk(client, f"Статья {i:02d}", body=f"Строка {i} одна.\nСтрока {i} две.")
    _mk(client, "Черновик", visible=False)
    r = client.get("/api/content/channels/publications")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 10
    assert all(not it["pinned"] for it in items)


def test_pinned_first_max3(client) -> None:
    for i in range(5):
        _mk(client, f"Обычная {i}", body=f"Текст {i}.")
    for i in range(4):
        # 4-й закреп сервер отклонит (валидация «не больше 3»)
        _mk(client, f"Закреп {i}", pinned=(i < 3), body=f"Закреплённый текст {i}.")
    r = client.get("/api/content/channels/publications")
    items = r.json()["items"]
    assert len(items) == 9                                # 5 обычных + 4 «Закреп N» (3 закреплены)
    assert sum(1 for it in items if it["pinned"]) == 3    # лимит закрепа
    assert all(it["pinned"] for it in items[:3])          # закрепы сверху


def test_excerpt_three_lines(client) -> None:
    from app.publications_store import list_visible

    _mk(client, "Статья", body="Строка А.\nСтрока Б.\nСтрока В.\nСтрока Г — не анонс.")
    it = list_visible()[0]
    r = client.get("/api/content/channels/publications")
    assert "Строка Г" not in r.json()["items"][0]["excerpt"]
    assert "Строка А" in r.json()["items"][0]["excerpt"]


def test_drafts_hidden(client) -> None:
    _mk(client, "Черновик скрыт", visible=False)
    _mk(client, "Публикация видна", visible=True)
    r = client.get("/api/content/channels/publications")
    titles = [it["title"] for it in r.json()["items"]]
    assert "Публикация видна" in titles
    assert "Черновик скрыт" not in titles


def test_admin_create_pinned_limit(client) -> None:
    for i in range(3):
        _mk(client, f"Закреп {i}", pinned=True)
    r = client.post("/admin/api/publications", headers=AUTH, json={
        "title": "Четвёртый закреп", "pinned": True,
        "body_html": "текст", "published_at": "2026-11-21",
    })
    assert r.status_code == 400
    assert "Закреплённых уже 3" in r.json()["error"]


def test_admin_title_validation(client) -> None:
    r = client.post("/admin/api/publications", headers=AUTH,
                    json={"title": "", "body_html": "текст"})
    assert r.status_code == 400
    r2 = client.post("/admin/api/publications", headers=AUTH,
                     json={"title": "x" * 101, "body_html": "текст"})
    assert r2.status_code == 400


def test_admin_delete_and_list(client) -> None:
    pid = _mk(client, "Удаляемая")
    r = client.delete(f"/admin/api/publications/{pid}", headers=AUTH)
    assert r.status_code == 200 and r.json()["removed"] is True
    assert client.get("/admin/api/publications", headers=AUTH).json()["items"] == []


def test_channels_page_renders_feed(client) -> None:
    _mk(client, "Видимая статья", visible=True, body="Текст статьи.\nЕщё строка.")
    _mk(client, "Скрытая статья", visible=False)
    html = client.get("/channels/").text
    assert "pub-feed" in html
    assert "Видимая статья" not in html  # лента рендерится клиентом из API
