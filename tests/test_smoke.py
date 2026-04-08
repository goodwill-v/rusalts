from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import app


def test_health() -> None:
    c = TestClient(app)
    r = c.get("/health")
    assert r.status_code == 200
    assert r.json().get("status") == "ok"


def test_chat_trigger_path() -> None:
    c = TestClient(app)
    r = c.post("/api/chat", json={"message": "Дай чек-лист как перейти в мах", "platform": "web"})
    assert r.status_code == 200
    data = r.json()
    assert data.get("used_llm") is False
    assert data.get("trigger_id")
    assert data.get("reply")


def test_kb_search() -> None:
    c = TestClient(app)
    r = c.get("/api/kb/search", params={"q": "что такое мах", "limit": 3})
    assert r.status_code == 200
    items = r.json().get("items")
    assert isinstance(items, list)
    assert len(items) >= 1

