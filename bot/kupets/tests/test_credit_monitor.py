"""Счётчик кредитов и пороги алертов (SQLite, переживает «рестарт»)."""
from __future__ import annotations

import services.credit_monitor as cm
import services.sqlite_store as store
from config import Config


def _tmp(monkeypatch, tmp_path):
    db = str(tmp_path / "state.db")
    monkeypatch.setattr(store.Config, "STATE_DB_PATH", db, raising=False)
    monkeypatch.setattr(cm.Config, "FIRECRAWL_MONTHLY_CREDITS", 100, raising=False)
    monkeypatch.setattr(cm.Config, "CREDITS_WARN_PCT", 80, raising=False)
    monkeypatch.setattr(cm.Config, "CREDITS_CRIT_PCT", 95, raising=False)
    return db


def test_usage_levels(monkeypatch, tmp_path):
    _tmp(monkeypatch, tmp_path)
    cm.record_search_credits("firecrawl", 20)
    cm.record_search_credits("firecrawl", 59)
    assert cm.usage_snapshot()["level"] == "ok"  # 79%
    cm.record_search_credits("firecrawl", 2)
    assert cm.usage_snapshot()["level"] == "warn"  # 81% (>=80, <95)
    cm.record_search_credits("firecrawl", 15)
    assert cm.usage_snapshot()["level"] == "crit"  # 96% (>=95, <100)
    cm.record_search_credits("firecrawl", 5)
    assert cm.usage_snapshot()["level"] == "exhausted"  # 101%


def test_credits_survive_reopen(monkeypatch, tmp_path):
    db = _tmp(monkeypatch, tmp_path)
    cm.record_search_credits("firecrawl", 6)
    assert store.credits_spent("firecrawl", path=db) == 6.0  # новое соединение = «рестарт»


def test_exhausted_blocks_and_alert_fires(monkeypatch, tmp_path):
    _tmp(monkeypatch, tmp_path)
    sent = {}
    monkeypatch.setattr(
        "services.max_notify.send_max", lambda text: sent.setdefault("max", text) or True
    )
    cm.record_search_credits("firecrawl", 100)
    snap = cm.check_and_alert()
    assert snap["level"] == "exhausted"
    assert "max" in sent  # МАКС-алерт ушёл
    assert cm.user_notice() is not None  # пользователю — вежливое пояснение


def test_alert_cooldown(monkeypatch, tmp_path):
    _tmp(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(
        "services.max_notify.send_max", lambda text: calls.append(text) or True
    )
    cm.record_search_credits("firecrawl", 100)
    cm.check_and_alert()
    cm.check_and_alert()  # второй раз за кулдаун — повторного сообщения нет
    assert len(calls) == 1


def test_user_notice_none_below_threshold(monkeypatch, tmp_path):
    _tmp(monkeypatch, tmp_path)
    cm.record_search_credits("firecrawl", 10)
    assert cm.user_notice() is None
