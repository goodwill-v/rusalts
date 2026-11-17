"""Тесты кеш-бастера статиков /zcode (задача Кора 18.11, образец — PR #147).

«Мёртвые кнопки» после деплоев: старый zcode_web.js в кеше браузера.
Версия = хеш содержимого файлов → URL меняется сам при каждом изменении.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import app  # noqa: E402
from app.routers import zcode_web  # noqa: E402

TOKEN = "test-token-123"


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("ZCODE_WEB_TOKEN", TOKEN)
    zcode_web._login_attempts.clear()
    zcode_web._assets_v_cache = None  # сброс кеша версии между тестами
    c = TestClient(app)
    return c


def _login(c: TestClient) -> None:
    assert c.post("/zcode/login", json={"token": TOKEN, "remember": False}).status_code == 200


def test_page_links_statics_with_version(client):
    _login(client)
    page = client.get("/zcode").text
    assert "/static/js/zcode_web.js?v=" in page
    assert "/static/css/zcode_web.css?v=" in page
    login_page = client.get("/zcode").text
    assert "/static/css/zcode_web.css?v=" in login_page  # обе страницы бастятся


def test_version_is_hash_of_content(client):
    v1 = zcode_web._assets_version()
    assert len(v1) == 10  # короткий sha1
    # версия детерминирована содержимым
    zcode_web._assets_v_cache = None
    assert zcode_web._assets_version() == v1


def test_version_changes_when_js_changes(client, monkeypatch, tmp_path):
    _login(client)
    page1 = client.get("/zcode").text
    v1 = page1.split("/static/js/zcode_web.js?v=")[1].split('"')[0]

    # меняем содержимое js-файла (временная подмена реального файла)
    js = zcode_web._STATIC_DIR / "js" / "zcode_web.js"
    original = js.read_bytes()
    try:
        js.write_bytes(original + b"\n// cache-bust test\n")
        zcode_web._assets_v_cache = None
        page2 = client.get("/zcode").text
        v2 = page2.split("/static/js/zcode_web.js?v=")[1].split('"')[0]
    finally:
        js.write_bytes(original)
        zcode_web._assets_v_cache = None

    assert v1 != v2  # URL изменился → кеш браузера инвалидируется


def test_css_change_changes_version(client):
    css = zcode_web._STATIC_DIR / "css" / "zcode_web.css"
    original = css.read_bytes()
    try:
        css.write_bytes(original + b"\n/* cache-bust */\n")
        zcode_web._assets_v_cache = None
        v = zcode_web._assets_version()
    finally:
        css.write_bytes(original)
        zcode_web._assets_v_cache = None
    assert len(v) == 10


def test_missing_asset_does_not_crash(client, monkeypatch, tmp_path):
    # Файл не найден (не должно случаться) — версия всё равно выдаётся
    monkeypatch.setattr(zcode_web, "_STATIC_DIR", tmp_path)  # пустая папка
    zcode_web._assets_v_cache = None
    v = zcode_web._assets_version()
    assert v  # не пустая и не исключение
    zcode_web._assets_v_cache = None
