"""Тесты веб-чата ZCode (ТЗ §2.7): auth, rate-limit, список сессий, отправка (моки)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import app  # noqa: E402
from app.routers import zcode_web  # noqa: E402

TOKEN = "test-token-123"


class FakeChannel:
    """Мок zcode-канала: список/строки/отправка без реального zcode-server."""

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def list_sessions(self, workspace: str) -> list[dict]:
        return [
            {
                "sessionId": "sess_wait",
                "title": "Ждёт ответа",
                "sessionKind": "interactive",
                "status": "waiting",
                "workspace": {"workspacePath": workspace},
                "createdAt": 1000,
                "updatedAt": 5000,
            },
            {
                "sessionId": "sess_run",
                "title": "Пишет",
                "sessionKind": "interactive",
                "status": "running",
                "workspace": {"workspacePath": workspace},
                "createdAt": 1000,
                "updatedAt": 4000,
            },
            {
                "sessionId": "sess_sub",
                "title": "Субагент (не показываем)",
                "sessionKind": "subagent_child",
                "status": "idle",
                "workspace": {"workspacePath": workspace},
                "createdAt": 1000,
                "updatedAt": 3000,
            },
        ]

    async def conversation_rows(self, session_id, workspace, before_row_id=None, limit=60):
        rows = [
            {"kind": "turnHeader", "rowId": 1, "origin": "userInput"},
            {"kind": "userInput", "rowId": 2, "text": "вопрос пользователя"},
            {
                "kind": "assistantText", "rowId": 3,
                "text": "Ответ с кодом:\n```python\nprint(1)\n```",
                "state": "complete",
            },
            {
                "kind": "toolCall", "rowId": 4, "toolName": "Bash", "status": "running",
                "inputText": "x" * 1000,
            },
        ]
        if before_row_id is not None:
            rows = [r for r in rows if r["rowId"] < before_row_id]
        return {"rows": rows, "atSeq": 10}

    async def send_prompt(self, session_id, workspace, text):
        self.sent.append((session_id, text))
        return {"sessionId": session_id, "accepted": True, "stateRevision": 1}

    async def create_session(self, workspace, title=None):
        return {
            "session": {
                "sessionId": "sess_new",
                "title": title or "",
                "sessionKind": "interactive",
                "status": "idle",
                "workspace": {"workspacePath": workspace},
                "createdAt": 1,
                "updatedAt": 1,
            }
        }

    async def session_lifecycle(self, session_id, workspace):
        return {"sessionId": session_id, "state": "idle", "generation": 1, "revision": 1}


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("ZCODE_WEB_TOKEN", TOKEN)
    fake = FakeChannel()
    monkeypatch.setattr(zcode_web, "get_channel", lambda: fake)
    zcode_web._login_attempts.clear()
    c = TestClient(app)
    c.fake_channel = fake  # type: ignore[attr-defined]
    return c


def _login(c: TestClient, token: str = TOKEN, remember: bool = False):
    return c.post("/zcode/login", json={"token": token, "remember": remember})


# ── Аутентификация (ТЗ §2.1) ────────────────────────────────────────────────

def test_no_token_configured_returns_404(monkeypatch):
    monkeypatch.delenv("ZCODE_WEB_TOKEN", raising=False)
    c = TestClient(app)
    assert c.get("/zcode").status_code == 404
    assert c.get("/zcode/api/sessions").status_code == 404


def test_page_without_cookie_shows_login(client):
    r = client.get("/zcode")
    assert r.status_code == 200
    assert "Токен доступа" in r.text


def test_api_without_cookie_401(client):
    r = client.get("/zcode/api/sessions")
    assert r.status_code == 401


def test_login_wrong_token_401(client):
    assert _login(client, "wrong").status_code == 401


def test_login_ok_sets_httponly_cookie(client):
    r = _login(client)
    assert r.status_code == 200
    cookie = r.headers.get("set-cookie", "")
    assert "httponly" in cookie.lower()
    assert "samesite=lax" in cookie.lower()
    assert "zcode_web_session=" in cookie
    # TTL по умолчанию — 12 часов
    assert "max-age=43200" in cookie.lower()


def test_login_remember_two_weeks(client):
    cookie = _login(client, remember=True).headers.get("set-cookie", "")
    assert "max-age=1209600" in cookie.lower()  # 14 дней (ТЗ §3.3)


def test_page_with_cookie_shows_app(client):
    assert _login(client).status_code == 200
    r = client.get("/zcode")
    assert r.status_code == 200
    assert "zc-list" in r.text  # разметка приложения, не логин


def test_cookie_invalidates_on_token_change(client, monkeypatch):
    _login(client)
    monkeypatch.setenv("ZCODE_WEB_TOKEN", "another-token")
    assert client.get("/zcode/api/sessions").status_code == 401


def test_login_rate_limit(client):
    for _ in range(5):
        assert _login(client, "wrong").status_code == 401
    assert _login(client, "wrong").status_code == 429  # 6-я попытка — отказ


def test_security_headers(client):
    r = client.get("/zcode")
    assert r.headers.get("x-robots-tag") == "noindex, nofollow"
    assert "no-store" in r.headers.get("cache-control", "")


def test_robots_txt_disallows_zcode(client):
    r = client.get("/robots.txt")
    assert r.status_code == 200
    assert "Disallow: /zcode" in r.text


def test_logout_clears_cookie(client):
    _login(client)
    assert client.get("/zcode/api/sessions").status_code == 200
    client.post("/zcode/logout")
    assert client.get("/zcode/api/sessions").status_code == 401


# ── Список сессий и диалог (моки) ──────────────────────────────────────────

def test_sessions_list_filters_and_badges(client):
    _login(client)
    data = client.get("/zcode/api/sessions").json()
    ids = [s["sessionId"] for s in data["sessions"]]
    assert "sess_sub" not in ids  # субагентские сессии скрыты
    by_id = {s["sessionId"]: s for s in data["sessions"]}
    assert by_id["sess_wait"]["waiting"] is True
    # «ждут вас» наверху списка
    assert data["sessions"][0]["sessionId"] == "sess_wait"


def test_rows_public_shape(client):
    _login(client)
    r = client.get("/zcode/api/sessions/sess_x/rows?workspace=/opt/alt")
    assert r.status_code == 200
    rows = r.json()["rows"]
    by_kind = {row["rowId"]: row for row in rows}
    assert by_kind[2]["text"] == "вопрос пользователя"
    assert by_kind[3]["state"] == "complete"
    # inputText длинного tool-вызова обрезан
    assert len(by_kind[4]["inputText"]) <= 400


def test_send_requires_text_and_passes_to_channel(client):
    _login(client)
    assert client.post(
        "/zcode/api/sessions/sess_x/send",
        json={"text": "", "workspace": "/opt/alt"},
    ).status_code == 400
    ok = client.post(
        "/zcode/api/sessions/sess_x/send",
        json={"text": "привет агенту", "workspace": "/opt/alt"},
    )
    assert ok.status_code == 200
    assert ok.json() == {"ok": True}
    assert client.fake_channel.sent == [("sess_x", "привет агенту")]


def test_create_session_returns_new_session(client):
    _login(client)
    r = client.post("/zcode/api/sessions", json={"title": "Тестовая сессия"})
    assert r.status_code == 200
    body = r.json()
    assert body["session"]["sessionId"] == "sess_new"
    # Имя при создании API не принимает — UI честно сообщает, что название
    # появится из первого сообщения (ТЗ §3.1).
    assert body["titleFromFirstMessage"] is True


def test_stream_requires_auth(client):
    r = client.get("/zcode/api/stream", params={"session_id": "sess_x", "workspace": "/opt/alt"})
    assert r.status_code == 401


def test_client_has_no_token_in_page(client):
    _login(client)
    page = client.get("/zcode").text
    assert TOKEN not in page  # токен не попадает в разметку
