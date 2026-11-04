"""Тесты контроля доменов ВежПома (domain_watch), PR-5.

Чистые функции без сети: разбор RDAP/WHOIS, дни до срока, пороги,
нормализация ввода. Дедуп порогов и суточный обход — на временной БД
с подставной send_fn, фиксированной датой «сегодня» и msk_hour.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bot" / "vezpom"))

import db as vezdb  # noqa: E402
import digest as vzdigest  # noqa: E402
import domain_watch as dw  # noqa: E402
import vezlogic  # noqa: E402


TODAY = date(2026, 11, 4)


def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(vezdb, "DB_PATH", str(tmp_path / "vezpom_domain.db"))
    vezdb.connect().close()


def _e(days):
    """Дата окончания через days дней от фиксированного «сегодня»."""
    return TODAY + timedelta(days=days)


class Collector:
    def __init__(self):
        self.sent = []

    def __call__(self, chat_id, text):
        self.sent.append((chat_id, text))


# ---- _parse_date: форматы дат RDAP и WHOIS ----

def test_parse_date_iso_with_z():
    assert dw._parse_date("2027-04-01T17:40:36Z") == date(2027, 4, 1)


def test_parse_date_iso_with_offset():
    assert dw._parse_date("2027-04-01T17:40:36+03:00") == date(2027, 4, 1)


def test_parse_date_plain_and_dotted():
    assert dw._parse_date("2027-04-01") == date(2027, 4, 1)
    assert dw._parse_date("01.04.2027") == date(2027, 4, 1)
    assert dw._parse_date("2027.04.01") == date(2027, 4, 1)


def test_parse_date_garbage():
    assert dw._parse_date("") is None
    assert dw._parse_date(None) is None
    assert dw._parse_date("not a date") is None
    assert dw._parse_date("2027-13-99") is None


# ---- parse_rdap_expiry: форматы RDAP-ответов ----

def test_rdap_verisign_com_sample():
    """Живой формат rdap.verisign.com (.com через rdap.org, снят 04.11.2026)."""
    data = {
        "objectClassName": "domain",
        "ldhName": "EXAMPLE.COM",
        "events": [
            {"eventAction": "registration", "eventDate": "1995-08-14T04:00:00Z"},
            {"eventAction": "expiration", "eventDate": "2027-08-13T04:00:00Z"},
            {"eventAction": "last changed", "eventDate": "2026-08-14T08:01:43Z"},
            {"eventAction": "last update of RDAP database",
             "eventDate": "2026-11-04T16:25:19Z"},
        ],
    }
    assert dw.parse_rdap_expiry(data) == date(2027, 8, 13)


def test_rdap_capitalized_action_and_offset():
    data = {"events": [
        {"eventAction": "Transfer", "eventDate": "2026-01-01T00:00:00+00:00"},
        {"eventAction": "Expiration", "eventDate": "2026-12-24T15:12:23+00:00"},
    ]}
    assert dw.parse_rdap_expiry(data) == date(2026, 12, 24)


def test_rdap_without_expiration_returns_none():
    assert dw.parse_rdap_expiry({"events": [
        {"eventAction": "registration", "eventDate": "1995-08-14T04:00:00Z"}]}) is None
    assert dw.parse_rdap_expiry({}) is None
    assert dw.parse_rdap_expiry({"events": None}) is None
    assert dw.parse_rdap_expiry({"events": []}) is None
    assert dw.parse_rdap_expiry(None) is None
    assert dw.parse_rdap_expiry("junk") is None


# ---- parse_whois_expiry: форматы WHOIS-ответов ----

def test_whois_tcinet_ru_sample():
    """Живой формат whois.tcinet.ru (.ru/.su/.рф), снят 04.11.2026."""
    text = (
        "domain:        RUSALTS.RU\n"
        "nserver:       ns1.timeweb.ru.\n"
        "state:         REGISTERED, DELEGATED, UNVERIFIED\n"
        "registrar:     TIMEWEB-RU\n"
        "paid-till:     2027-04-01T17:40:36Z\n"
        "free-date:     2027-05-02\n"
    )
    # paid-till (оплата), а не free-date (грейс-период после удаления)
    assert dw.parse_whois_expiry(text) == date(2027, 4, 1)


def test_whois_icann_registry_expiry():
    text = ("Domain Name: EXAMPLE.ORG\n"
            "Registry Expiry Date: 2027-03-10T04:00:00Z\n"
            "Updated Date: 2026-02-01T11:30:00Z\n")
    assert dw.parse_whois_expiry(text) == date(2027, 3, 10)


def test_whois_without_expiry_returns_none():
    assert dw.parse_whois_expiry("") is None
    assert dw.parse_whois_expiry(None) is None
    assert dw.parse_whois_expiry("No entries found\n") is None
    # только free-date — срок оплаты неизвестен, молчим
    assert dw.parse_whois_expiry("free-date: 2027-05-02\n") is None


# ---- days_left и classify_domain ----

def test_days_left_bounds():
    assert dw.days_left(date(2026, 11, 5), today=TODAY) == 1
    assert dw.days_left(date(2026, 11, 4), today=TODAY) == 0
    assert dw.days_left(date(2026, 1, 1), today=TODAY) == 0  # истёкшие — 0
    assert dw.days_left(None) is None


def test_classify_domain_thresholds():
    assert dw.classify_domain(31) is None        # далеко — молчим и не шумим
    assert dw.classify_domain(None) is None
    assert dw.classify_domain(30)["level"] == 30
    assert dw.classify_domain(15)["level"] == 30
    assert dw.classify_domain(14)["level"] == 14
    assert dw.classify_domain(9)["level"] == 14  # между порогами — ближний пройденный
    assert dw.classify_domain(7)["level"] == 7
    assert dw.classify_domain(2)["level"] == 7
    assert dw.classify_domain(1)["level"] == 1
    assert dw.classify_domain(0)["level"] == 1
    assert dw.classify_domain(9)["days"] == 9


def test_domain_thresholds_match_vezlogic():
    assert vezlogic.DOMAIN_THRESHOLDS == (30, 14, 7, 1)


# ---- normalize_domain / to_ascii_domain ----

def test_normalize_domain_basic():
    assert dw.normalize_domain("Company.RU") == "company.ru"
    assert dw.normalize_domain("  example.com  ") == "example.com"
    assert dw.normalize_domain("https://www.example.com/path?q=1") == "example.com"
    assert dw.normalize_domain("example.com:8080") == "example.com"
    assert dw.normalize_domain("example.com.") == "example.com"
    assert dw.normalize_domain("магазин.рф") == "магазин.рф"


def test_normalize_domain_rejects_junk():
    assert dw.normalize_domain("") is None
    assert dw.normalize_domain("example") is None            # без точки
    assert dw.normalize_domain("192.168.1.10") is None       # IP не домен
    assert dw.normalize_domain("not a domain") is None       # пробел
    assert dw.normalize_domain("-bad-.ru") is None           # дефисы по краям метки
    assert dw.normalize_domain("a..b.ru") is None            # пустая метка
    assert dw.normalize_domain("doma_in.ru") is None         # подчёркивание


def test_to_ascii_domain_punycode():
    assert dw.to_ascii_domain("мир.рф") == "xn--h1ahn.xn--p1ai"
    assert dw.to_ascii_domain("company.ru") == "company.ru"


# ---- db: домены ----

def test_domains_crud(tmp_path, monkeypatch):
    _tmp_db(tmp_path, monkeypatch)
    assert vezdb.add_domain(1, "company.ru") is True
    assert vezdb.add_domain(1, "company.ru") is False        # дубль (UNIQUE)
    vezdb.add_domain(1, "example.com")
    assert len(vezdb.list_domains(1)) == 2
    vezdb.set_domain_due(1, "company.ru", "2027-04-01")
    rows = {d["domain"]: d for d in vezdb.list_domains(1)}
    assert rows["company.ru"]["due_date"] == "2027-04-01"
    assert vezdb.remove_domain(1, "example.com") is True
    assert vezdb.remove_domain(1, "example.com") is False
    assert len(vezdb.list_domains(1)) == 1


def test_list_active_domains_skips_inactive(tmp_path, monkeypatch):
    _tmp_db(tmp_path, monkeypatch)
    vezdb.create_user(7)                                     # триал 14 дней — активен
    vezdb.create_user(8)
    conn = vezdb.connect()
    conn.execute("UPDATE users SET paid_until=1 WHERE chat_id=8")
    conn.commit()
    conn.close()
    vezdb.add_domain(7, "active.ru")
    vezdb.add_domain(8, "expired.ru")
    active = {(c, d) for c, d in vezdb.list_active_domains()}
    assert (7, "active.ru") in active
    assert (8, "expired.ru") not in active


# ---- сообщения ----

def test_format_domain_warning_plural_and_date():
    msg = dw.format_domain_warning("company.ru", 9, _e(9))
    assert msg.startswith("🟡") and "через 9 дней" in msg and "company.ru" in msg
    assert "13.11.2026" in msg
    assert "через 1 день" in dw.format_domain_warning("company.ru", 1, _e(1))
    assert "через 22 дня" in dw.format_domain_warning("company.ru", 22, _e(22))
    assert "истёк" in dw.format_domain_warning("company.ru", 0, _e(0))


def test_format_domain_status_icons():
    assert dw.format_domain_status("x.ru", _e(100), 100).startswith("🟢")
    assert dw.format_domain_status("x.ru", _e(9), 9).startswith("🟡")
    assert dw.format_domain_status("x.ru", _e(0), 0).startswith("🔴")
    assert dw.format_domain_status("x.ru", None, None).startswith("🟡")


# ---- дедуп порогов и анти-шум (process_expiry, временная БД) ----

def test_process_expiry_threshold_dedup(tmp_path, monkeypatch):
    _tmp_db(tmp_path, monkeypatch)
    vezdb.create_user(1)
    vezdb.add_domain(1, "company.ru")
    out = Collector()
    noon = 12  # вне тихих часов

    # добавлен задолго до порогов — тишина (как после команды /domain)
    assert dw.process_expiry(1, "company.ru", _e(40), out, noon, today=TODAY) == "ok"
    assert out.sent == []

    # вошли в зону: первое сообщение на пороге 30 (по факту 25 дн.)
    assert dw.process_expiry(1, "company.ru", _e(25), out, noon, today=TODAY) == "notified"
    assert len(out.sent) == 1 and "через 25 дней" in out.sent[0][1]

    # между порогами — тишина
    assert dw.process_expiry(1, "company.ru", _e(20), out, noon, today=TODAY) == "silent"
    assert dw.process_expiry(1, "company.ru", _e(15), out, noon, today=TODAY) == "silent"
    assert len(out.sent) == 1

    # порог 14 — одно сообщение, затем тишина до 7
    assert dw.process_expiry(1, "company.ru", _e(14), out, noon, today=TODAY) == "notified"
    assert dw.process_expiry(1, "company.ru", _e(13), out, noon, today=TODAY) == "silent"
    assert dw.process_expiry(1, "company.ru", _e(9), out, noon, today=TODAY) == "silent"
    assert len(out.sent) == 2

    # пороги 7 и 1 — по одному сообщению
    assert dw.process_expiry(1, "company.ru", _e(7), out, noon, today=TODAY) == "notified"
    assert dw.process_expiry(1, "company.ru", _e(6), out, noon, today=TODAY) == "silent"
    assert dw.process_expiry(1, "company.ru", _e(1), out, noon, today=TODAY) == "notified"
    assert dw.process_expiry(1, "company.ru", _e(0), out, noon, today=TODAY) == "silent"
    assert len(out.sent) == 4
    assert "через 1 день" in out.sent[-1][1]
    assert "оплачена до" in out.sent[-1][1]


def test_process_expiry_quiet_hours_defer_until_morning(tmp_path, monkeypatch):
    """Тихие часы: порог откладывается, утром доставляется."""
    _tmp_db(tmp_path, monkeypatch)
    vezdb.create_user(1)
    vezdb.add_domain(1, "night.ru")
    out = Collector()
    # 03:00 МСК (тихие часы) — ничего не пишем и не отправляем
    assert dw.process_expiry(1, "night.ru", _e(14), out, msk_hour=3, today=TODAY) == "deferred"
    assert out.sent == []
    assert vezdb.list_open_warnings(1) == []
    # 12:00 МСК — утренний обход доставляет отложенный порог
    assert dw.process_expiry(1, "night.ru", _e(14), out, msk_hour=12, today=TODAY) == "notified"
    assert len(out.sent) == 1
    assert len(vezdb.list_open_warnings(1)) == 1
    # повтор — тишина
    assert dw.process_expiry(1, "night.ru", _e(13), out, msk_hour=12, today=TODAY) == "silent"
    assert len(out.sent) == 1


def test_process_expiry_renewal_resets_thresholds(tmp_path, monkeypatch):
    _tmp_db(tmp_path, monkeypatch)
    vezdb.create_user(1)
    vezdb.add_domain(1, "company.ru")
    out = Collector()
    noon = 12
    assert dw.process_expiry(1, "company.ru", _e(14), out, noon, today=TODAY) == "notified"
    assert dw.process_expiry(1, "company.ru", _e(7), out, noon, today=TODAY) == "notified"
    # продлили: срок отскочил на год — тишина, предупреждение закрыто
    assert dw.process_expiry(1, "company.ru", _e(365), out, noon, today=TODAY) == "ok"
    assert len(out.sent) == 2
    assert vezdb.list_open_warnings(1) == []
    # новый цикл: порог 30 снова активен (прыжок > 60 сут сбросил last_level)
    assert dw.process_expiry(1, "company.ru", _e(30), out, noon, today=TODAY) == "notified"
    assert len(out.sent) == 3


def test_process_expiry_far_future_no_warning_in_light(tmp_path, monkeypatch):
    _tmp_db(tmp_path, monkeypatch)
    vezdb.create_user(1)
    vezdb.add_domain(1, "far.ru")
    out = Collector()
    assert dw.process_expiry(1, "far.ru", _e(100), out, 12, today=TODAY) == "ok"
    assert out.sent == []
    assert vezdb.list_open_warnings(1) == []               # 🟡 в светофоре нет
    assert len(vezdb.list_events(1, since_ts=0)) == 0


def test_process_expiry_failure_once_then_recovery(tmp_path, monkeypatch):
    _tmp_db(tmp_path, monkeypatch)
    vezdb.create_user(1)
    vezdb.add_domain(1, "x.ru")
    out = Collector()
    noon = 12
    # реестр не ответил — одно сообщение + открытое 🟡-предупреждение
    assert dw.process_expiry(1, "x.ru", None, out, noon, today=TODAY) == "failed"
    assert len(out.sent) == 1 and "Не удалось узнать срок" in out.sent[0][1]
    assert len(vezdb.list_open_warnings(1)) == 1
    # повторная неудача — молчание (дедуп)
    assert dw.process_expiry(1, "x.ru", None, out, noon, today=TODAY) == "failed"
    assert len(out.sent) == 1
    # реестр ответил: срок далеко → предупреждение закрыто, без сообщений
    assert dw.process_expiry(1, "x.ru", _e(200), out, noon, today=TODAY) == "ok"
    assert len(out.sent) == 1
    assert vezdb.list_open_warnings(1) == []


# ---- суточный обход и планировщик ----

def test_check_all_sweep_end_to_end(tmp_path, monkeypatch):
    """Обход всех доменов без сети (fetch_expiry подменён), день вне тихих часов."""
    _tmp_db(tmp_path, monkeypatch)
    vezdb.create_user(5)
    vezdb.add_domain(5, "soon.ru")
    vezdb.add_domain(5, "far.ru")
    expiries = {"soon.ru": _e(7), "far.ru": _e(200)}
    monkeypatch.setattr(dw, "fetch_expiry", lambda d, timeout=20: expiries.get(d))
    out = Collector()
    stats = dw.check_all(out, pause=0, today=TODAY, msk_hour=12)
    assert stats == {"checked": 2, "notified": 1, "failed": 0}
    assert len(out.sent) == 1 and "soon.ru" in out.sent[0][1]
    # due_date доменов обновлён из проверки
    rows = {d["domain"]: d for d in vezdb.list_domains(5)}
    assert rows["soon.ru"]["due_date"] == _e(7).isoformat()
    assert rows["far.ru"]["due_date"] == _e(200).isoformat()


def test_sweep_tick_idempotent(tmp_path, monkeypatch):
    """Идемпотентность как у светофора (PR-3): один проход в МСК-сутки."""
    _tmp_db(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(dw, "check_all", lambda send_fn, pause=1.0, today=None, msk_hour=None:
                        calls.append(1))
    day_ts = 1800000000            # 11:00:00 МСК — позже 09:30
    dw._sweep_tick(None, now_ts=day_ts - 8 * 3600)  # 03:00 МСК — до времени обхода
    assert len(calls) == 0
    dw._sweep_tick(None, now_ts=day_ts)             # 11:00 МСК — первый обход
    assert len(calls) == 1
    dw._sweep_tick(None, now_ts=day_ts + 1800)      # тот же день — молчим
    assert len(calls) == 1
    dw._sweep_tick(None, now_ts=day_ts + 86400)     # новые сутки — снова работает
    assert len(calls) == 2


# ---- интеграция со светофором и еженедельным отчётом (digest) ----

def test_traffic_light_domain_warning(tmp_path, monkeypatch):
    _tmp_db(tmp_path, monkeypatch)
    vezdb.add_monitor(1, "https://x.ru", "VeZhPom chat1: https://x.ru", 10)
    vezdb.update_monitor_status("VeZhPom chat1: https://x.ru", "up")
    vezdb.upsert_warning(1, "domain", "company.ru", days=9)
    text = vzdigest.build_traffic_light(1)
    assert chr(0x1F7E1) in text and "домен продлевается через 9 дн." in text


def test_traffic_light_domain_unknown_and_expired(tmp_path, monkeypatch):
    _tmp_db(tmp_path, monkeypatch)
    vezdb.add_monitor(1, "https://x.ru", "VeZhPom chat1: https://x.ru", 10)
    vezdb.update_monitor_status("VeZhPom chat1: https://x.ru", "up")
    vezdb.upsert_warning(1, "domain", "unknown.ru")   # срок неизвестен (due_ts=None)
    vezdb.upsert_warning(1, "domain", "dead.ru", days=0)  # истёкший домен
    text = vzdigest.build_traffic_light(1)
    assert "не удалось определить" in text
    assert "истёк" in text


def test_weekly_report_domain_recommendation(tmp_path, monkeypatch):
    _tmp_db(tmp_path, monkeypatch)
    vezdb.upsert_warning(1, "domain", "company.ru", days=9)
    text = vzdigest.build_weekly_report(1)
    assert "Незакрытых предупреждений: 1" in text
    assert "продлить домены" in text
