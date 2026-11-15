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
    # фокус теста — только светофор: еженедельный отчёт выключен, иначе тест
    # падает по дням недели (в понедельник тик шлёт и отчёт — баг 09.11)
    vezdb.set_prefs(7, light_time="00:00", weekly_report=0)
    sent = []
    digest._tick(lambda cid, text: sent.append((cid, text)))
    assert len(sent) == 1 and sent[0][0] == 7
    digest._tick(lambda cid, text: sent.append((cid, text)))  # повтор в тот же день
    assert len(sent) == 1  # второй раз не шлём


# ---- журнал за 30 дней (/history), PR «журнал с метриками» ----

def test_monthly_report_breakdown(tmp_path, monkeypatch):
    _tmp_db(tmp_path, monkeypatch)
    now = int(time.time())
    vezdb.add_event(1, 10, "https://a.ru", "incident", "down", "",
                    started_at=now - 7200, resolved_at=now - 6600)   # 10 мин
    vezdb.add_event(1, 10, "https://a.ru", "incident", "down", "",
                    started_at=now - 3600, resolved_at=now - 3000)   # 10 мин
    vezdb.add_event(1, 10, "https://b.ru", "incident", "down", "",
                    started_at=now - 600, resolved_at=now - 300)     # 5 мин
    text = digest.build_report(1, days=30, title="📋 Журнал за 30 дней:")
    assert "Журнал за 30 дней" in text and "Сбоев: 3" in text
    assert "Разбивка по ресурсам" in text
    # a.ru падал 2 раза и дольше всех — первая строка разбивки
    first = text.split("Разбивка по ресурсам:")[1].strip().splitlines()[0]
    assert "https://a.ru" in first and "2 раз" in first


def test_weekly_report_has_recommendations_but_monthly_has_breakdown(tmp_path, monkeypatch):
    _tmp_db(tmp_path, monkeypatch)
    now = int(time.time())
    vezdb.upsert_warning(1, "ssl", "https://x.ru", days=5)
    week = digest.build_weekly_report(1)
    assert "Рекомендация" in week and "Разбивка" not in week
    vezdb.add_event(1, 10, "https://x.ru", "incident", "down", "",
                    started_at=now - 300, resolved_at=now - 60)
    month = digest.build_report(1, days=30, title="📋 Журнал за 30 дней:")
    assert "Разбивка" in month and "Рекомендация" not in month


# ---- свип статусов: авто-закрытие зависших инцидентов (fix 09.11) ----

def test_refresh_statuses_closes_stale_incident(tmp_path, monkeypatch):
    import time as _time
    _tmp_db(tmp_path, monkeypatch)
    now = int(_time.time())
    vezdb.create_user(9)
    vezdb.add_monitor(9, "https://stale.ru", "VeZhPom chat9: https://stale.ru", 99)
    vezdb.update_monitor_status("VeZhPom chat9: https://stale.ru", "down")
    vezdb.add_event(9, 99, "https://stale.ru", "incident", "down", "",
                    started_at=now - 400000)  # открытый down 4+ суток

    class FakeKuma:
        def get_monitor_status(self, mid):
            return "up", 42, "OK"   # сайт на самом деле жив

    monkeypatch.setattr(digest, "kuma", FakeKuma())
    closed = digest.refresh_statuses()
    assert closed == 1
    ev = vezdb.list_events(9, since_ts=now - 500000)[0]
    assert ev["resolved_at"] is not None            # инцидент закрыт
    assert vezdb.list_monitors(9)[0]["last_status"] == "up"  # статус обновлён


def test_status_sweep_day_idempotent(tmp_path, monkeypatch):
    _tmp_db(tmp_path, monkeypatch)
    vezdb.create_user(7)
    vezdb.add_monitor(7, "https://y.ru", "VeZhPom chat7: https://y.ru", 11)
    calls = []
    monkeypatch.setattr(digest, "refresh_statuses", lambda: calls.append(1) or 0)
    ts = 1800000000 + 3600   # 11:00 МСК
    digest._status_sweep_tick(now_ts=ts - 9 * 3600)  # 02:00 МСК — рано
    assert len(calls) == 0
    digest._status_sweep_tick(now_ts=ts)             # первый проход
    assert len(calls) == 1
    digest._status_sweep_tick(now_ts=ts + 60)        # тот же день — молчим
    assert len(calls) == 1
