"""Тесты анти-шума и журнала ВежПома (vezlogic + db), PR-2.

Бот живёт в bot/vezpom/ — подключаем путь явно (как app.* в других тестах).
Чистые функции без env/сети; db-тесты используют временную БД.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bot" / "vezpom"))

import vezlogic  # noqa: E402
import db as vezdb  # noqa: E402


# ---- classify_event ----

def test_classify_http_down():
    data = {
        "monitor": {"name": "VeZhPom chat1: https://x.ru", "type": "http", "url": "https://x.ru"},
        "heartbeat": {"status": 0, "ping": None, "msg": "Timeout"},
    }
    r = vezlogic.classify_event(data)
    assert r["kind"] == vezlogic.KIND_INCIDENT
    assert r["status"] == "down"


def test_classify_http_up():
    data = {
        "monitor": {"name": "VeZhPom chat1: https://x.ru", "type": "http", "url": "https://x.ru"},
        "heartbeat": {"status": 1, "ping": 42, "msg": "OK"},
    }
    r = vezlogic.classify_event(data)
    assert r["kind"] == vezlogic.KIND_INCIDENT
    assert r["status"] == "up"
    assert "42 мс" in r["detail"]


def test_classify_legacy_down():
    data = {"status": "down", "name": "X", "heartbeat": {"status": 0}}
    r = vezlogic.classify_event(data)
    assert r["kind"] == vezlogic.KIND_INCIDENT
    assert r["status"] == "down"


def test_classify_legacy_up_without_down_before_is_info():
    data = {"status": "up", "previous_status": "up", "name": "X"}
    r = vezlogic.classify_event(data)
    assert r["kind"] == vezlogic.KIND_OTHER


def test_classify_push_backup_down_up():
    down = {"monitor": {"name": "VeZhPom chat1: BACKUP", "type": "push"},
            "heartbeat": {"status": 0}}
    up = {"monitor": {"name": "VeZhPom chat1: BACKUP", "type": "push"},
          "heartbeat": {"status": 1}}
    assert vezlogic.classify_event(down)["kind"] == vezlogic.KIND_BACKUP
    assert vezlogic.classify_event(down)["status"] == "down"
    assert vezlogic.classify_event(up)["status"] == "up"


def test_classify_ssl_cert_expiry():
    data = {
        "monitor": {"name": "VeZhPom chat1: https://x.ru · SSL", "type": "http",
                    "url": "https://x.ru"},
        "heartbeat": None,
        "msg": "[VeZhPom chat1: https://x.ru · SSL][https://x.ru]"
               " Let's Encrypt certificate x.ru will be expired in 9 days",
    }
    r = vezlogic.classify_event(data)
    assert r["kind"] == vezlogic.KIND_SSL
    assert r["status"] == "info"
    assert r["days"] == 9


def test_classify_empty_is_other():
    r = vezlogic.classify_event({})
    assert r["kind"] == vezlogic.KIND_OTHER


# ---- тихие часы и should_forward ----

def test_quiet_hours_boundaries():
    assert vezlogic.is_quiet_hour(23) is True
    assert vezlogic.is_quiet_hour(2) is True
    assert vezlogic.is_quiet_hour(7) is True
    assert vezlogic.is_quiet_hour(8) is False
    assert vezlogic.is_quiet_hour(22) is False


def _incident(status):
    return {"kind": vezlogic.KIND_INCIDENT, "status": status, "days": None,
            "name": "x", "target": "x", "message": "", "detail": ""}


def test_should_forward_incident_always_even_in_quiet_hours():
    assert vezlogic.should_forward(_incident("down"), msk_hour=3) is True
    assert vezlogic.should_forward(_incident("up"), msk_hour=3) is True


def _ssl(days):
    return {"kind": vezlogic.KIND_SSL, "status": "info", "days": days,
            "name": "x", "target": "x", "message": "", "detail": ""}


def test_should_forward_ssl_respects_quiet_hours():
    assert vezlogic.should_forward(_ssl(9), msk_hour=12) is True
    assert vezlogic.should_forward(_ssl(9), msk_hour=23) is False


def test_should_forward_domain_threshold_dedup():
    d14 = {"kind": vezlogic.KIND_DOMAIN, "status": "info", "days": 14,
           "name": "x", "target": "x", "message": "", "detail": ""}
    d7 = {"kind": vezlogic.KIND_DOMAIN, "status": "info", "days": 7,
          "name": "x", "target": "x", "message": "", "detail": ""}
    assert vezlogic.should_forward(d7, msk_hour=12, last_level=14) is True   # новый порог
    assert vezlogic.should_forward(d7, msk_hour=12, last_level=7) is False   # уже шла
    assert vezlogic.should_forward(d14, msk_hour=12, last_level=7) is False  # старый порог
    assert vezlogic.should_forward(d14, msk_hour=12) is True                 # ничего не шла


# ---- format_event ----

def test_format_incident_down_red():
    msg = vezlogic.format_event(_incident("down") | {"name": "site.ru", "detail": " (отклик 5 мс)"})
    assert msg.startswith(chr(0x1F534)) and "site.ru" in msg


def test_format_ssl_message():
    msg = vezlogic.format_event(_ssl(9))
    assert "истекает через 9 дн." in msg
    assert chr(0x1F510) in msg


def test_format_backup_down_yellow():
    r = {"kind": vezlogic.KIND_BACKUP, "status": "down", "days": None,
         "name": "VeZhPom chat1: BACKUP", "target": "backup", "message": "", "detail": ""}
    msg = vezlogic.format_event(r)
    assert "Резервная копия не создалась" in msg


# ---- db: журнал и предупреждения (временная БД) ----

def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(vezdb, "DB_PATH", str(tmp_path / "vezpom_test.db"))
    conn = vezdb.connect()
    conn.close()


def test_events_roundtrip(tmp_path, monkeypatch):
    _tmp_db(tmp_path, monkeypatch)
    now = int(time.time())
    vezdb.add_event(1, 10, "https://x.ru", "incident", "down", "t", started_at=now)
    ev = vezdb.list_events(1, since_ts=now - 60)
    assert len(ev) == 1 and ev[0]["status"] == "down" and ev[0]["resolved_at"] is None
    assert vezdb.close_open_incident(1, "https://x.ru", now + 60) == 1
    assert vezdb.close_open_incident(1, "https://x.ru", now + 60) == 0  # уже закрыт
    ev = vezdb.list_events(1, since_ts=now - 60)
    assert ev[0]["resolved_at"] == now + 60


def test_warning_upsert_and_resolve(tmp_path, monkeypatch):
    _tmp_db(tmp_path, monkeypatch)
    w = vezdb.upsert_warning(1, "ssl", "https://x.ru", days=9)
    assert w["last_level"] == 9 and w["open"] == 1 and w["due_ts"] is not None
    # тот же срок повторно — last_level не растёт
    w = vezdb.upsert_warning(1, "ssl", "https://x.ru", days=9)
    assert w["last_level"] == 9
    # продление (срок отскочил вперёд > 60 сут) — сброс порогов в 99
    w = vezdb.upsert_warning(1, "ssl", "https://x.ru", days=90)
    assert w["last_level"] == 99
    vezdb.resolve_warning(1, "ssl", "https://x.ru")
    assert vezdb.list_open_warnings(1) == []


def test_backup_warning_no_days(tmp_path, monkeypatch):
    _tmp_db(tmp_path, monkeypatch)
    w = vezdb.upsert_warning(1, "backup", "VeZhPom chat1: BACKUP")
    assert w["due_ts"] is None and w["open"] == 1
    assert len(vezdb.list_open_warnings(1)) == 1
    vezdb.resolve_warning(1, "backup", "VeZhPom chat1: BACKUP")
    assert vezdb.list_open_warnings(1) == []
