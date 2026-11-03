"""Тесты светофора и еженедельного отчёта ВежПома (digest), PR-3."""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bot" / "vezpom"))

import db as vezdb  # noqa: E402
import digest  # noqa: E402


def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(vezdb, "DB_PATH", str(tmp_path / "vezpom_digest.db"))
    vezdb.connect().close()


def test_traffic_light_green(tmp_path, monkeypatch):
    _tmp_db(tmp_path, monkeypatch)
    vezdb.add_monitor(1, "https://x.ru", "VeZhPom chat1: https://x.ru", 10)
    vezdb.update_monitor_status("VeZhPom chat1: https://x.ru", "up")
    text = digest.build_traffic_light(1)
    assert text and chr(0x1F7E2) in text and "Всё в норме" in text


def test_traffic_light_red_and_yellow(tmp_path, monkeypatch):
    _tmp_db(tmp_path, monkeypatch)
    vezdb.add_monitor(1, "https://x.ru", "VeZhPom chat1: https://x.ru", 10)
    vezdb.update_monitor_status("VeZhPom chat1: https://x.ru", "down")
    vezdb.upsert_warning(1, "ssl", "https://x.ru", days=9)
    text = digest.build_traffic_light(1)
    assert chr(0x1F534) in text and "нет ответа" in text
    assert chr(0x1F7E1) in text and "SSL истекает через 9 дн." in text


def test_traffic_light_none_when_nothing_configured(tmp_path, monkeypatch):
    _tmp_db(tmp_path, monkeypatch)
    assert digest.build_traffic_light(1) is None


def test_weekly_report_quiet_week(tmp_path, monkeypatch):
    _tmp_db(tmp_path, monkeypatch)
    text = digest.build_weekly_report(1)
    assert "Сбоев не было" in text


def test_weekly_report_with_downtime(tmp_path, monkeypatch):
    _tmp_db(tmp_path, monkeypatch)
    now = int(time.time())
    vezdb.add_event(1, 10, "https://x.ru", "incident", "down", "",
                    started_at=now - 3780, resolved_at=now - 180)  # ~1 ч простой
    vezdb.upsert_warning(1, "ssl", "https://x.ru", days=5)
    text = digest.build_weekly_report(1)
    assert "Сбоев: 1" in text and "суммарный простой" in text
    assert "https://x.ru" in text and "продлить SSL" in text


def test_parse_hhmm():
    assert digest._parse_hhmm("09:30") == (9, 30)
    assert digest._parse_hhmm("bad") == (9, 0)
    assert digest._parse_hhmm("10:05", "09:00") == (10, 5)


def test_tick_sends_once_per_day(tmp_path, monkeypatch):
    _tmp_db(tmp_path, monkeypatch)
    vezdb.create_user(7)  # триал 14 дней → активен
    vezdb.add_monitor(7, "https://y.ru", "VeZhPom chat7: https://y.ru", 11)
    vezdb.update_monitor_status("VeZhPom chat7: https://y.ru", "up")
    vezdb.set_prefs(7, light_time="00:00")  # время уже наступило в любые часы суток
    sent = []
    digest._tick(lambda cid, text: sent.append((cid, text)))
    assert len(sent) == 1 and sent[0][0] == 7
    digest._tick(lambda cid, text: sent.append((cid, text)))  # повтор в тот же день
    assert len(sent) == 1  # второй раз не шлём
