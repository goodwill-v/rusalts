"""Тесты служебной команды СКВ (!skv / /skv) и защиты владельцем.

Опция «Контроль работы агента на сервере»: команда запускает диагностику СКВ
и присылает отчёт; доступна только владельцу сервиса (CHAT_ID).
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bot" / "vezpom"))

os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("CHAT_ID", "1")

import db as vezdb  # noqa: E402
import vezpom_webhook as w  # noqa: E402


def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(vezdb, "DB_PATH", str(tmp_path / "t.db"))
    vezdb.connect().close()


class _FakeCompleted:
    def __init__(self, out="", err="", timeout=False):
        self.stdout = out
        self.stderr = err
        self._timeout = timeout


def test_skv_owner_gets_report(tmp_path, monkeypatch):
    """Владелец (chat_id == CHAT_ID) получает отчёт СКВ."""
    _tmp_db(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(w, "send", lambda c, t: calls.append((c, t)))

    def fake_run(*a, **k):
        return _FakeCompleted(out="ИТОГ: 🟢9 🟡2 🔴0\n")

    monkeypatch.setattr(w.subprocess, "run", fake_run)

    w.handle_command("!skv", w.CHAT_ID)

    # первое сообщение — «запускаю», последнее — отчёт
    assert calls[0][0] == w.CHAT_ID
    assert "диагностик" in calls[0][1].lower()
    assert "ИТОГ" in calls[-1][1]


def test_skv_alias_slash(tmp_path, monkeypatch):
    """/skv — та же команда."""
    _tmp_db(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(w, "send", lambda c, t: calls.append((c, t)))
    monkeypatch.setattr(w.subprocess, "run", lambda *a, **k: _FakeCompleted(out="ok"))
    w.handle_command("/skv", w.CHAT_ID)
    assert "ИТОГ" in calls[-1][1] or "ok" in calls[-1][1]


def test_skv_non_owner_denied(tmp_path, monkeypatch):
    """Посторонний НЕ получает диагностику сервера."""
    _tmp_db(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(w, "send", lambda c, t: calls.append((c, t)))

    def boom(*a, **k):
        raise AssertionError("subprocess не должен вызываться для постороннего")

    monkeypatch.setattr(w.subprocess, "run", boom)

    w.handle_command("!skv", w.CHAT_ID + 999)

    assert len(calls) == 1
    assert "владельц" in calls[0][1].lower()


def test_skv_timeout_handled(tmp_path, monkeypatch):
    """Таймаут диагностики — вежливое сообщение, без падения."""
    _tmp_db(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(w, "send", lambda c, t: calls.append((c, t)))

    def fake_run(*a, **k):
        raise w.subprocess.TimeoutExpired(cmd="skv", timeout=60)

    monkeypatch.setattr(w.subprocess, "run", fake_run)
    w.handle_command("!skv", w.CHAT_ID)

    assert "60 секунд" in calls[-1][1]
