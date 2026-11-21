"""Страницы /laws/ и /channels/ (ТЗ Алика 16.11): контент-блоки вместо заглушек,
редактирование из админки («📝 Контент»), каналы с пустой ссылкой неактивны."""

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
    from app import site_settings as ss
    (data / "content").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ss, "_PATH", data / "content" / "site_settings.json")
    import pathlib
    for attr in ("CONTENT_ITEMS_DIR", "CONTENT_ARCHIVE_DIR", "CONTENT_PUBLISHED_DIR",
                 "CONTENT_PUBLISHED_SITE_DIR", "CONTENT_PUBLISHED_SITE_INDEX_PATH",
                 "CONTENT_SEQ_PATH", "MONITORING_DIR", "CONTENT_DIR"):
        if hasattr(config, attr):
            monkeypatch.setattr(config, attr, pathlib.Path(data) / attr.lower())
    from app.main import app

    return TestClient(app)


def test_laws_page_renders_without_stub(client) -> None:
    r = client.get("/laws/")
    assert r.status_code == 200
    assert "Правовая база" in r.text
    assert "будет добавлено позже" not in r.text          # заглушки нет
    assert "О персональных данных" in r.text              # карточка 152-ФЗ из SCHEMA
    assert "не являются юридической консультацией" in r.text  # правовая оговорка


def test_channels_page_renders_without_stub(client) -> None:
    r = client.get("/channels/")
    assert r.status_code == 200
    assert "Где нас читать" in r.text
    assert "Новосёлы ЦД" in r.text
    assert "Очерк Алик" in r.text                        # лента публикаций


def test_channels_cta_top_and_hidden_tier23(client) -> None:
    """CTA «Нужен бот в вашем канале?» — в начале страницы, под кнопками
    шапки; карточки 2–3 ярусов (Сайт/VC/Пикабу/WikiClaw) скрыты (Шеф 20.11)."""
    html = client.get("/channels/").text
    # CTA и H1 в одном блоке ch-top (на одном уровне, Шеф 20.11)
    assert '<div class="ch-top">' in html
    assert html.find("Нужен бот в вашем канале?") != -1
    assert html.find(">Каналы</h1>") != -1
    # скрытые каналы не рендерятся
    for hidden in ("Сайт rusalts.ru", "VC.ru", "Пикабу", "WikiClaw"):
        assert f'<span class="nm">{hidden}</span>' not in html, hidden
    # активные остались: Дзен (неактивная карточка «скоро»), ВК, МАКС
    for active in ("Дзен", "ВКонтакте", "МАКС-канал"):
        assert active in html
    assert "https://vk.com/alternat777" in html


def test_vk_linked_but_unregistered_channels_inactive(client) -> None:
    html = client.get("/channels/").text
    # ВК и МАКС активны (ссылки заданы в SCHEMA)
    assert "https://vk.com/alternat777" in html
    # Дзен и VC без ссылки — карточки неактивны («скоро»)
    assert "скоро" in html


def test_admin_content_blocks_has_new_pages(client) -> None:
    d = client.get("/admin/api/content-blocks", headers=AUTH).json()
    ids = [p["id"] for p in d["pages"]]
    assert "laws" in ids and "channels" in ids
    laws_schema = {f["key"]: f for f in d["schema"]["laws"]}
    assert "law152_text" in laws_schema and "cta_btn_url" in laws_schema
    ch_schema = {f["key"]: f for f in d["schema"]["channels"]}
    assert "dzen_url" in ch_schema and "cd_btn_url" in ch_schema


def test_admin_save_changes_laws_page(client) -> None:
    r = client.post("/admin/api/content-blocks", headers=AUTH, json={
        "page": "laws",
        "blocks": {"h1": "Правовая база для бизнеса", "lead": "Наш специальный лид <b>с выделением</b>."},
    })
    assert r.status_code == 200
    html = client.get("/laws/").text
    assert "Правовая база для бизнеса" in html
    assert "<b>с выделением</b>" in html                  # HTML рендерится, не экранируется


def test_other_pages_unaffected(client) -> None:
    for path in ("/legal/privacy/", "/news/", "/techologis/", "/request/", "/diagnostics/"):
        assert client.get(path).status_code == 200, path


def test_laws_sections_render_admin_values(client) -> None:
    """Регресс 18.11: topics/samples/sources тексты и ссылки карточек —
    из админки, не захардкожены в шаблоне (Шеф правил — страница стояла)."""
    r = client.post("/admin/api/content-blocks", headers=AUTH, json={
        "page": "laws",
        "blocks": {
            "samples_text": "Документы Платформы АЛТ: <a href='/legal/privacy/'>Политика ПДн</a>. Шаблоны в АЛТ-эксперт.",
            "sources_text": "Наш источник: <a href='http://pravo.gov.ru/'>pravo.gov.ru</a>.",
            "topics_text": "<ul><li>Наша тема из админки</li></ul>",
            "law168_link": "http://pravo.gov.ru/168fz",
        },
    })
    assert r.status_code == 200, r.text
    html = client.get("/laws/").text
    assert "Документы Платформы АЛТ" in html            # текст секции из админки
    assert "Шаблоны (на юрпроверку)" not in html         # дефолт больше не мешает
    assert "Наш источник" in html                        # sources тоже
    assert "Наша тема из админки" in html                # topics тоже
    assert 'href="http://pravo.gov.ru/168fz"' in html    # ссылка карточки закона из админки


def test_cleared_field_hides_element(client) -> None:
    """Семантика очистки (Шеф 21.11): сохранил пустую вводную строку —
    элемент скрывается на сайте, дефолт не возвращается."""
    client.post("/admin/api/content-blocks", headers=AUTH, json={
        "page": "channels", "blocks": {"lead": ""},
    })
    html = client.get("/channels/").text
    assert "Здесь мы живём и разговариваем" not in html
    assert '<p class="lead">' not in html


def test_cleared_field_restorable(client) -> None:
    """Вернул текст в поле — элемент снова на сайте."""
    client.post("/admin/api/content-blocks", headers=AUTH, json={
        "page": "channels", "blocks": {"lead": "Снова на связи."},
    })
    html = client.get("/channels/").text
    assert "Снова на связи." in html
