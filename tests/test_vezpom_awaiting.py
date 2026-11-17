"""Тесты двухшагового сценария (команда → адрес), UX 09.11."""
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


def test_all_options_two_step(tmp_path, monkeypatch):
    _tmp_db(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(w, "send", lambda c, t: calls.append(("send", t)))
    monkeypatch.setattr(w, "_remove_monitor_flow", lambda c, t: calls.append(("mon_off", t)))
    monkeypatch.setattr(w, "_remove_domain_flow", lambda c, t: calls.append(("dom_off", t)))
    monkeypatch.setattr(w, "_link_flow", lambda c, t: calls.append(("link", t)))
    monkeypatch.setattr(w, "_scan_flow", lambda c, t: calls.append(("scan", t)))
    monkeypatch.setattr(w, "_add_monitor_flow", lambda c, t: calls.append(("monitor", t)))

    for cmd, addr, tag in [
        ("/monitor_off", "https://x.ru", "mon_off"),
        ("/domain_off", "company.ru", "dom_off"),
        ("/link", "http://evil.tk", "link"),
        ("/scan", "mysite.ru", "scan"),
        ("/monitor", "https://y.ru", "monitor"),
    ]:
        w.handle_command(cmd, 1)          # команда без аргумента — список/приглашение
        assert calls[-1][0] == "send"
        w.handle_command(addr, 1)         # адрес следующим сообщением
        assert calls[-1] == (tag, addr), (cmd, calls[-1])


def test_command_inside_mode_does_not_swallow(tmp_path, monkeypatch):
    _tmp_db(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(w, "send", lambda c, t: calls.append(("send", t)))
    monkeypatch.setattr(w, "_remove_monitor_flow", lambda c, t: calls.append(("mon_off", t)))

    w.handle_command("/monitor_off", 1)   # режим включён
    w.handle_command("/status", 1)        # команда — выполняется штатно
    assert any(c[0] == "send" and "Тариф" in c[1] for c in calls)
