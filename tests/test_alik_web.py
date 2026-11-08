"""Тесты таба «Алик» (ТЗ-дополнение §1.6): история из БД, стрим, rate-limit, auth.

Gateway замокан (мокается stream_chat) — реальный openclaw не вызывается.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import alik_store, config  # noqa: E402
from app.alik_rpc import AlikError  # noqa: E402
from app.main import app  # noqa: E402
from app.routers import zcode_web  # noqa: E402

TOKEN = "test-token-123"


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("ZCODE_WEB_TOKEN", TOKEN)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)  # БД истории — во временную папку
    zcode_web._login_attempts.clear()
    zcode_web._alik_send_attempts.clear()
    c = TestClient(app)
    c.tmp_path = tmp_path  # type: ignore[attr-defined]
    return c


def _login(c: TestClient) -> None:
    r = c.post("/zcode/login", json={"token": TOKEN, "remember": False})
    assert r.status_code == 200


def _parse_sse(body: str) -> list[dict]:
    events = []
    for raw in body.split("\n\n"):
        for line in raw.split("\n"):
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))
    return events


# ── Хранилище: контекст для gateway (ТЗ §3 — ключевая инженерная точка) ─────

def test_context_is_last_30_in_order(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    for i in range(35):
        alik_store.add_message("c1", "user" if i % 2 == 0 else "assistant", f"msg {i}")
    ctx = alik_store.context_messages("c1")
    assert len(ctx) == 30
    assert ctx[0]["content"] == "msg 5"     # первые 5 отрезаны
    assert ctx[-1]["content"] == "msg 34"   # последнее — последним
    assert ctx[0]["role"] == "assistant" and ctx[1]["role"] == "user"  # i=5,6


def test_chats_are_independent(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    alik_store.add_message("a", "user", "вопрос для чата A")
    alik_store.add_message("b", "user", "вопрос для чата B")
    assert [m["content"] for m in alik_store.history("a")] == ["вопрос для чата A"]
    assert [m["content"] for m in alik_store.history("b")] == ["вопрос для чата B"]


def test_chats_list_titles_and_order(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    alik_store.add_message("old", "user", "первый вопрос", ts=1000.0)
    alik_store.add_message("old", "assistant", "ответ", ts=1001.0)
    alik_store.add_message("new", "user", "свежий вопрос", ts=2000.0)
    chats = alik_store.chats()
    assert chats[0]["chat_id"] == "new"           # свежий наверху
    assert chats[0]["title"] == "свежий вопрос"   # название из первого сообщения
    assert chats[1]["messages"] == 2


# ── API: история/список/новый чат ───────────────────────────────────────────

def test_alik_api_requires_auth(client):
    assert client.get("/zcode/api/alik/chats").status_code == 401
    assert client.get("/zcode/api/alik/history?chat_id=x").status_code == 401
    assert client.post("/zcode/api/alik/chats", json={}).status_code == 401
    assert client.post("/zcode/api/alik/send", json={"chat_id": "x", "text": "п"}).status_code == 401


def test_new_chat_and_history_roundtrip(client):
    _login(client)
    chat_id = client.post("/zcode/api/alik/chats", json={}).json()["chat_id"]
    assert chat_id
    body = '{"type":"delta","text":"отве","x":1}\n\n'  # форма SSE не важна тут
    r = client.get(f"/zcode/api/alik/history?chat_id={chat_id}")
    assert r.status_code == 200
    assert r.json()["messages"] == []


# ── Отправка: стрим и запись в БД (мок gateway) ─────────────────────────────

def test_send_streams_deltas_and_saves_history(client, monkeypatch):
    _login(client)
    chat_id = client.post("/zcode/api/alik/chats", json={}).json()["chat_id"]

    captured = {}

    async def fake_stream(messages, cid):
        captured["messages"] = messages
        captured["cid"] = cid
        yield "Привет! "
        yield "Я Алик."

    monkeypatch.setattr("app.routers.zcode_web.stream_chat", fake_stream)

    r = client.post("/zcode/api/alik/send", json={"chat_id": chat_id, "text": "привет Алик"})
    assert r.status_code == 200
    events = _parse_sse(r.text)
    assert [e["type"] for e in events] == ["delta", "delta", "done"]
    assert "".join(e.get("text", "") for e in events) == "Привет! Я Алик."

    # контекст, ушедший в gateway: системного мусора нет, только БД+новое сообщение
    assert captured["cid"] == chat_id
    assert [m["role"] for m in captured["messages"]] == ["user"]

    # история в БД: пользователь + ответ Алика (стрим записан)
    msgs = client.get(f"/zcode/api/alik/history?chat_id={chat_id}").json()["messages"]
    assert [(m["role"], m["content"]) for m in msgs] == [
        ("user", "привет Алик"),
        ("assistant", "Привет! Я Алик."),
    ]


def test_send_context_assembles_history(client, monkeypatch):
    _login(client)
    chat_id = client.post("/zcode/api/alik/chats", json={}).json()["chat_id"]
    alik_store.add_message(chat_id, "user", "первый вопрос")
    alik_store.add_message(chat_id, "assistant", "первый ответ")

    captured = {}

    async def fake_stream(messages, cid):
        captured["messages"] = messages
        yield "ок"

    monkeypatch.setattr("app.routers.zcode_web.stream_chat", fake_stream)
    r = client.post("/zcode/api/alik/send", json={"chat_id": chat_id, "text": "что я писал выше?"})
    assert r.status_code == 200
    assert captured["messages"] == [
        {"role": "user", "content": "первый вопрос"},
        {"role": "assistant", "content": "первый ответ"},
        {"role": "user", "content": "что я писал выше?"},
    ]


def test_send_gateway_error_rolls_back_user_message(client, monkeypatch):
    _login(client)
    chat_id = client.post("/zcode/api/alik/chats", json={}).json()["chat_id"]

    async def failing_stream(messages, cid):
        raise AlikError("gateway HTTP 404: not found")
        yield  # pragma: no cover

    monkeypatch.setattr("app.routers.zcode_web.stream_chat", failing_stream)
    r = client.post("/zcode/api/alik/send", json={"chat_id": chat_id, "text": "тест"})
    events = _parse_sse(r.text)
    assert events[-1]["type"] == "error"
    assert "404" in events[-1]["message"]
    # сообщение пользователя откачено — ретрай не задублирует
    assert client.get(f"/zcode/api/alik/history?chat_id={chat_id}").json()["messages"] == []


def test_send_without_gateway_token_is_honest_error(client, monkeypatch):
    _login(client)
    monkeypatch.delenv("OPENCLAW_GATEWAY_TOKEN", raising=False)
    chat_id = client.post("/zcode/api/alik/chats", json={}).json()["chat_id"]

    real_token = alik_store  # noqa: F841 — важно: stream_chat не мокаем, но токена нет
    async def passthrough(messages, cid):
        from app.alik_rpc import stream_chat as real
        async for d in real(messages, cid):
            yield d

    monkeypatch.setattr("app.routers.zcode_web.stream_chat", passthrough)
    r = client.post("/zcode/api/alik/send", json={"chat_id": chat_id, "text": "тест"})
    events = _parse_sse(r.text)
    assert events[-1]["type"] == "error"
    assert "OPENCLAW_GATEWAY_TOKEN" in events[-1]["message"]
    assert client.get(f"/zcode/api/alik/history?chat_id={chat_id}").json()["messages"] == []


def test_send_validation(client):
    _login(client)
    assert client.post("/zcode/api/alik/send", json={"chat_id": "", "text": "x"}).status_code == 400
    assert client.post("/zcode/api/alik/send", json={"chat_id": "c", "text": ""}).status_code == 400
    assert client.post(
        "/zcode/api/alik/send", json={"chat_id": "c", "text": "x" * 17000}
    ).status_code == 413


# ── Rate-limit (ТЗ-дополнение §1.5 — защита gateway) ────────────────────────

def test_alik_send_rate_limit(client, monkeypatch):
    _login(client)
    chat_id = client.post("/zcode/api/alik/chats", json={}).json()["chat_id"]

    async def fake_stream(messages, cid):
        yield "ок"

    monkeypatch.setattr("app.routers.zcode_web.stream_chat", fake_stream)
    for _ in range(5):
        r = client.post("/zcode/api/alik/send", json={"chat_id": chat_id, "text": "x"})
        assert r.status_code == 200
    assert client.post("/zcode/api/alik/send", json={"chat_id": chat_id, "text": "x"}).status_code == 429


# ── sessionKey (рекомендация Алика §2.4 — поле user, не заголовок) ──────────

def test_gateway_body_uses_user_field(monkeypatch):
    import httpx

    captured = {}

    class FakeResp:
        status_code = 200

        def aread(self):
            return asyncio.coroutine(lambda self: b"").__get__(self)()

        async def aiter_lines(self):
            yield 'data: {"choices":[{"delta":{"content":"x"}}]}'
            yield "data: [DONE]"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, method, url, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "tok")

    from app.alik_rpc import stream_chat

    async def run():
        chunks = []
        async for d in stream_chat([{"role": "user", "content": "привет"}], "abc123"):
            chunks.append(d)
        return chunks

    chunks = asyncio.run(run())
    assert chunks == ["x"]
    assert captured["json"]["user"] == "web:alik:abc123"  # стабильная сессия
    assert captured["json"]["stream"] is True
    assert captured["json"]["model"] == "openclaw"
    assert "X-OpenClaw-Session-Key" not in (captured["headers"] or {})  # не используем
    assert captured["headers"]["Authorization"] == "Bearer tok"
