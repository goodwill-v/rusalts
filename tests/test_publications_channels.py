"""Публикации (задача Шефа 11.11): раздельные «Отмена» Сайт/ВК, пустое окно
ВК → публикации ВК нет, автопубликация корпоративных черновиков по тумблеру."""

from __future__ import annotations

import sys
from base64 import b64encode
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config

AUTH = {"Authorization": "Basic " + b64encode(b"admin:20rusalt13").decode()}
SITE_TEXT = "Полный текст новости для сайта — достаточно длинный для валидации формы."
VK_TEXT = "Текст для ВКонтакте — короткая версия."


@pytest.fixture()
def client(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", data)
    # пути, прибинженные при первом импорте модулей, изолируем явно
    # контентные каталоги прибинжены в config при импорте — изолируем
    import pathlib
    for attr in ("CONTENT_ITEMS_DIR", "CONTENT_ARCHIVE_DIR", "CONTENT_PUBLISHED_DIR",
                 "CONTENT_PUBLISHED_SITE_DIR", "CONTENT_PUBLISHED_SITE_INDEX_PATH",
                 "CONTENT_SEQ_PATH", "MONITORING_DIR", "CONTENT_DIR"):
        if hasattr(config, attr):
            monkeypatch.setattr(config, attr, pathlib.Path(data) / attr.lower())
    from app.main import app

    return TestClient(app)


@pytest.fixture()
def vk_calls(monkeypatch):
    """Перехват публикации в ВК: фиксируем вызовы, реальный API не дёргаем."""
    calls = []

    async def fake_vk(item):
        calls.append(item.publication_id)
        return ("2026-11-10T00:00:00+00:00", 123, "https://vk.com/wall-1_123")

    from app import content_publish_flow

    monkeypatch.setattr(content_publish_flow, "publish_to_vk", fake_vk)
    return calls


def _submit(client, vk_text=VK_TEXT) -> str:
    # 10 пробелов проходят min_length, но после strip() окно ВК считается пустым
    payload_vk = vk_text if vk_text is not None else " " * 10
    r = client.post("/api/content/submit", json={
        "title": "Тестовая новость",
        "site_text": SITE_TEXT,
        "vk_text": payload_vk,
        "sources": [],
        "internal_note": "тест",
    })
    assert r.status_code == 200, r.text
    return r.json()["publication_id"]


def test_empty_vk_window_no_vk_publication(client, vk_calls) -> None:
    """Если в окне ВКонтакте нет текста — публикации ВК нет (без подстановки сайта)."""
    pid = _submit(client, vk_text=None)  # пустое окно ВК
    r = client.post(f"/api/content/queue/{pid}/approve", headers=AUTH)
    assert r.status_code == 200
    d = r.json()
    assert d["site_published"] is True
    assert d["vk_published"] is False
    assert vk_calls == []  # до ВК даже не пытались дойти


def test_filled_vk_publishes_both(client, vk_calls) -> None:
    pid = _submit(client)
    d = client.post(f"/api/content/queue/{pid}/approve", headers=AUTH).json()
    assert d["site_published"] is True
    assert d["vk_published"] is True
    assert vk_calls == [pid]


def test_channel_cancel_vk_only(client, vk_calls) -> None:
    pid = _submit(client)
    d = client.post(f"/api/content/queue/{pid}/approve", headers=AUTH,
                    json={"site_cancel": False, "vk_cancel": True}).json()
    assert d["site_published"] is True
    assert d["vk_published"] is False
    assert vk_calls == []


def test_channel_cancel_site_only(client, vk_calls) -> None:
    pid = _submit(client)
    d = client.post(f"/api/content/queue/{pid}/approve", headers=AUTH,
                    json={"site_cancel": True, "vk_cancel": False}).json()
    assert d["site_published"] is False
    assert d["vk_published"] is True


def test_apply_all_respects_channel_cancels(client, vk_calls) -> None:
    pid = _submit(client)
    r = client.post("/api/content/queue/apply-all", headers=AUTH, json={"commands": [
        {"publication_id": pid, "action": "publish", "site_cancel": True, "vk_cancel": False},
    ]})
    assert r.status_code == 200
    d = r.json()
    assert d["published"] == 1
    assert d["details"][0]["skipped"]["site"] is True
    assert d["details"][0]["skipped"]["vk"] is False


def test_common_cancel_still_removes_item(client) -> None:
    pid = _submit(client)
    r = client.post("/api/content/queue/apply-all", headers=AUTH, json={"commands": [
        {"publication_id": pid, "action": "cancel"},
    ]})
    assert r.json()["canceled"] == 1


def test_corporate_draft_auto_publish_by_toggle(client, tmp_path, monkeypatch, vk_calls) -> None:
    """«Режим публикаций» включён → корпоративный черновик публикуется сам."""
    import json as jsonlib

    from app import site_settings as ss

    # _PATH прибинжен при первом импорте модуля — изолируем явно
    ss._PATH.parent.mkdir(parents=True, exist_ok=True)
    ss._PATH.write_text(jsonlib.dumps({"auto_publish": True}))
    import app.routers.content as content_router

    async def fake_refine(publication_id):
        return True, ""

    monkeypatch.setattr(content_router, "refine_corporate_item_by_id", fake_refine)
    r = client.post("/api/content/corporate/save", headers=AUTH, json={
        "title": "Корпоративная новость",
        "site_text": SITE_TEXT,
        "vk_text": VK_TEXT,
        "sources": [],
    })
    assert r.status_code == 200, r.text
    pid = r.json()["publication_id"]
    # фоновый refine+публикация асинхронны — ждём до 5 с
    import time
    from app.content_store import load_item
    for _ in range(50):
        it = load_item(pid)
        if it.site_published_at_utc:
            break
        time.sleep(0.1)
    it = load_item(pid)
    assert it.site_published_at_utc, "черновик не опубликован по тумблеру"
    assert vk_calls == [pid]
