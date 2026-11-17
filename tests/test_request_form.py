"""Форма «Заявка — обращение» (решение Шефа 09.11): Имя*, контакт* (тлф/почта),
тема, описание; миграция topic/contact; страница контактов — кнопка на форму."""

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
    from app.routers import request_form

    # DB_PATH привязан при первом импорте модуля — изолируем явно
    monkeypatch.setattr(request_form, "DB_PATH", data / "requests.db")
    from app.main import app

    return TestClient(app)


def _requests_rows():
    import sqlite3

    from app.routers import request_form

    db = sqlite3.connect(str(request_form.DB_PATH))  # путь привязан при первом импорте модуля
    db.row_factory = sqlite3.Row
    return [dict(r) for r in db.execute("SELECT * FROM requests ORDER BY id")]


def test_form_page_renders(client) -> None:
    r = client.get("/request/")
    assert r.status_code == 200
    for needle in ("Ваше имя", "телефон или почта", "Тема обращения", "Описание заявки"):
        assert needle in r.text


def test_submit_email_contact(client, monkeypatch) -> None:
    import app.routers.request_form as rf

    sent = {}
    monkeypatch.setattr(rf, "send_request_email", lambda subject, body: sent.update(subject=subject, body=body))
    r = client.post("/request/", data={
        "name": "Тест", "contact": "a@b.ru", "topic": "Автоматизация",
        "message": "Нужен ИИ-ассистент", "page_ref": "test",
    })
    assert r.status_code == 200
    row = _requests_rows()[0]
    assert row["topic"] == "Автоматизация"
    assert row["contact"] == "a@b.ru"
    assert row["email"] == "a@b.ru"
    assert "Тема: Автоматизация" in sent["body"]
    assert "Телефон" not in sent["body"].split("Описание")[0].split("Контакт")[1].split("\n")[0]


def test_submit_phone_contact(client, monkeypatch) -> None:
    import app.routers.request_form as rf

    monkeypatch.setattr(rf, "send_request_email", lambda subject, body: None)
    r = client.post("/request/", data={
        "name": "Иван", "contact": "+7 900 123-45-67", "topic": "Вопрос",
        "message": "Как начать?", "page_ref": "test",
    })
    assert r.status_code == 200
    row = _requests_rows()[0]
    assert row["contact"] == "+7 900 123-45-67"
    assert row["email"] == ""  # телефон — email-колонка пустая


def test_contact_required(client, monkeypatch) -> None:
    import app.routers.request_form as rf

    monkeypatch.setattr(rf, "send_request_email", lambda subject, body: None)
    r = client.post("/request/", data={"name": "X", "contact": "", "message": "?"})
    assert r.status_code == 400
    assert "контакт" in r.text.lower()


def test_bad_contact_rejected(client, monkeypatch) -> None:
    import app.routers.request_form as rf

    monkeypatch.setattr(rf, "send_request_email", lambda subject, body: None)
    r = client.post("/request/", data={"name": "X", "contact": "не контакт", "message": "?"})
    assert r.status_code == 400
    assert "не распознан" in r.text


def test_contacts_section_has_request_button(client) -> None:
    html = client.get("/").text
    assert "/request/" in html
    assert "Заявка — обращение" in html
    assert "iframe (VK)" not in html
