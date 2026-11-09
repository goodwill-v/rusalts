#!/usr/bin/env python3
"""VeZhPom DB: users, subscriptions and monitors (SQLite).

Table users:
  chat_id      INTEGER PRIMARY KEY  -- MAX user id
  pm_id        TEXT                 -- saved YooKassa payment method id
  trial_start  INTEGER              -- unix ts of registration
  paid_until   INTEGER              -- unix ts, subscription valid until
  status       TEXT                 -- trial | active | expired | canceled
  notify_sent  INTEGER              -- 1 = expiry reminder already sent

Table monitors:
  id            INTEGER PRIMARY KEY AUTOINCREMENT
  chat_id       INTEGER              -- owner of the monitor
  target        TEXT                 -- URL or IP the user wants to watch
  monitor_name  TEXT                 -- name inside Uptime Kuma
  kuma_id       INTEGER              -- monitor id in Uptime Kuma
  kuma_server   TEXT DEFAULT 'alt'   -- инстанс Kuma; с 09.11 единый ALT
                                  -- (сервер ROL 195.208.2.63 выведен из эксплуатации)
  last_status   TEXT                 -- up | down (last known)
  created_at    INTEGER

Table domains (PR-5, контроль сроков доменов):
  chat_id       INTEGER              -- owner (владелец цели — chat id)
  domain        TEXT                 -- домен в нижнем регистре (UNIQUE с chat_id)
  due_date      TEXT                 -- последняя известная дата окончания (ISO)
  created_at    INTEGER

Table sweep_state (идемпотентность суточных обходов):
  name          TEXT PRIMARY KEY     -- 'domains' и т.п.
  last_day      INTEGER              -- номер МСК-суток последнего обхода
"""
import os
import sqlite3
import time

DB_PATH = os.environ.get("DB_PATH", "/opt/vezpom/vezpom.db")
# Дефолты; актуальные значения — из тарифа витрины (админка /admin/ → Биллинг,
# правки применяются без рестарта бота, TTL 60 с в vitrina_client).
TRIAL_DAYS = 14
SUBSCRIPTION_DAYS = 30

try:
    import vitrina_client as _vitrina
except Exception:  # нет модуля/зависимостей — работаем на константах
    _vitrina = None


def trial_days() -> int:
    """Триал из тарифа витрины, фолбэк — константа."""
    if _vitrina is not None:
        return _vitrina.get_trial_days(TRIAL_DAYS)
    return TRIAL_DAYS


def subscription_days() -> int:
    """Период подписки из тарифа витрины, фолбэк — константа."""
    if _vitrina is not None:
        return _vitrina.get_period_days(SUBSCRIPTION_DAYS)
    return SUBSCRIPTION_DAYS


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS users ("
        " chat_id INTEGER PRIMARY KEY,"
        " pm_id TEXT,"
        " trial_start INTEGER,"
        " paid_until INTEGER,"
        " status TEXT DEFAULT 'trial',"
        " notify_sent INTEGER DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS payments ("
        " payment_id TEXT PRIMARY KEY,"
        " chat_id INTEGER,"
        " amount REAL,"
        " currency TEXT DEFAULT 'RUB',"
        " status TEXT,"
        " kind TEXT,"
        " created_at INTEGER,"
        " updated_at INTEGER)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS monitors ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " chat_id INTEGER NOT NULL,"
        " target TEXT NOT NULL,"
        " monitor_name TEXT,"
        " kuma_id INTEGER,"
        " kuma_server TEXT DEFAULT 'alt',"
        " last_status TEXT,"
        " created_at INTEGER)"
    )
    # Миграции блока «контроль сроков» (PR-1): SSL- и push-мониторы
    cols = {row[1] for row in conn.execute("PRAGMA table_info(monitors)")}
    if "cert_kuma_id" not in cols:
        conn.execute("ALTER TABLE monitors ADD COLUMN cert_kuma_id INTEGER")
    if "push_kuma_id" not in cols:
        conn.execute("ALTER TABLE monitors ADD COLUMN push_kuma_id INTEGER")
    if "push_token" not in cols:
        conn.execute("ALTER TABLE monitors ADD COLUMN push_token TEXT")
    # warnings.note (решение №3 по PR-5): вердикт источника для сроков
    # ('not_found' = домена нет в реестре; '' = срок получен; NULL = нет метки)
    wcols = {row[1] for row in conn.execute("PRAGMA table_info(warnings)")}
    if wcols and "note" not in wcols:
        conn.execute("ALTER TABLE warnings ADD COLUMN note TEXT")
    # Журнал событий и открытые предупреждения (PR-2, анти-шум)
    # events: kind = incident | ssl | backup | domain; status = down | up | info
    conn.execute(
        "CREATE TABLE IF NOT EXISTS events ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " chat_id INTEGER NOT NULL,"
        " kuma_id INTEGER,"
        " target TEXT,"
        " kind TEXT NOT NULL,"
        " status TEXT NOT NULL,"
        " detail TEXT,"
        " started_at INTEGER,"
        " resolved_at INTEGER)"
    )
    # warnings: kind = ssl | domain | backup; last_level = отправленный порог (дней);
    # note = вердикт источника ('not_found' / '' / NULL)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS warnings ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " chat_id INTEGER NOT NULL,"
        " kind TEXT NOT NULL,"
        " target TEXT NOT NULL,"
        " due_ts INTEGER,"
        " last_level INTEGER DEFAULT 99,"
        " last_notified_at INTEGER,"
        " open INTEGER DEFAULT 1,"
        " note TEXT,"
        " UNIQUE(chat_id, kind, target))"
    )
    # Настройки оповещений (PR-3): светофор и еженедельный отчёт
    conn.execute(
        "CREATE TABLE IF NOT EXISTS prefs ("
        " chat_id INTEGER PRIMARY KEY,"
        " traffic_light INTEGER DEFAULT 1,"
        " light_time TEXT DEFAULT '09:00',"   # МСК
        " weekly_report INTEGER DEFAULT 1,"
        " weekly_day INTEGER DEFAULT 0,"      # 0 = понедельник
        " weekly_time TEXT DEFAULT '10:00',"  # МСК
        " last_light_sent INTEGER DEFAULT 0," # идемпотентность: не слать дважды в день
        " last_weekly_sent INTEGER DEFAULT 0)"
    )
    # Домены под контролем (PR-5): владелец — chat_id, срок знает domain_watch
    conn.execute(
        "CREATE TABLE IF NOT EXISTS domains ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " chat_id INTEGER NOT NULL,"
        " domain TEXT NOT NULL,"
        " due_date TEXT,"
        " created_at INTEGER,"
        " UNIQUE(chat_id, domain))"
    )
    # Идемпотентность суточных обходов (домены — раз в МСК-сутки, как светофор в PR-3)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sweep_state ("
        " name TEXT PRIMARY KEY,"
        " last_day INTEGER)"
    )
    return conn


def get_user(chat_id):
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM users WHERE chat_id=?", (chat_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    keys = ["chat_id", "pm_id", "trial_start", "paid_until", "status", "notify_sent"]
    return dict(zip(keys, row))


def create_user(chat_id):
    now = int(time.time())
    conn = connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO users (chat_id, trial_start, paid_until, status)"
            " VALUES (?, ?, ?, 'trial')",
            (chat_id, now, now + trial_days() * 86400),
        )
        conn.commit()
    finally:
        conn.close()


def set_pm(chat_id, pm_id):
    conn = connect()
    try:
        conn.execute("UPDATE users SET pm_id=?, status='active' WHERE chat_id=?", (pm_id, chat_id))
        conn.commit()
    finally:
        conn.close()


def extend_subscription(chat_id, days=None):
    if days is None:
        days = subscription_days()
    now = int(time.time())
    conn = connect()
    try:
        row = conn.execute("SELECT paid_until FROM users WHERE chat_id=?", (chat_id,)).fetchone()
        base = max(now, row[0] if row and row[0] else now)
        conn.execute(
            "UPDATE users SET paid_until=?, status='active', notify_sent=0 WHERE chat_id=?",
            (base + days * 86400, chat_id),
        )
        conn.commit()
    finally:
        conn.close()


def is_active(chat_id):
    user = get_user(chat_id)
    if user is None:
        create_user(chat_id)
        user = get_user(chat_id)
    return user["status"] != "canceled" and user["paid_until"] > int(time.time())


def mark_notify_sent(chat_id):
    conn = connect()
    try:
        conn.execute("UPDATE users SET notify_sent=1 WHERE chat_id=?", (chat_id,))
        conn.commit()
    finally:
        conn.close()


def cancel_subscription(chat_id):
    conn = connect()
    try:
        conn.execute("UPDATE users SET status='canceled', pm_id=NULL WHERE chat_id=?", (chat_id,))
        conn.commit()
    finally:
        conn.close()


PAYMENT_KEYS = ["payment_id", "chat_id", "amount", "currency", "status", "kind", "created_at", "updated_at"]


def upsert_payment(payment_id, chat_id, amount, currency="RUB", status="pending", kind=""):
    """Insert or update a payment in the journal (log of YooKassa payments)."""
    now = int(time.time())
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO payments (payment_id, chat_id, amount, currency, status, kind, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(payment_id) DO UPDATE SET"
            "  status=excluded.status, amount=excluded.amount, updated_at=excluded.updated_at",
            (payment_id, chat_id, amount, currency, status, kind, now, now),
        )
        conn.commit()
    finally:
        conn.close()


def _row_to_payment(row):
    if row is None:
        return None
    return dict(zip(PAYMENT_KEYS, row))


def get_payment(payment_id):
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM payments WHERE payment_id=?", (payment_id,)).fetchone()
    finally:
        conn.close()
    return _row_to_payment(row)


def last_succeeded_payment(chat_id):
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM payments WHERE chat_id=? AND status='succeeded'"
            " ORDER BY created_at DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
    finally:
        conn.close()
    return _row_to_payment(row)


def list_payments(chat_id, limit=20):
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM payments WHERE chat_id=? ORDER BY created_at DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_payment(r) for r in rows]


MONITOR_KEYS = ["id", "chat_id", "target", "monitor_name", "kuma_id", "kuma_server",
                "last_status", "created_at", "cert_kuma_id", "push_kuma_id", "push_token"]


def _row_to_monitor(row):
    if row is None:
        return None
    return dict(zip(MONITOR_KEYS, row))


def add_monitor(chat_id, target, monitor_name, kuma_id, kuma_server="alt",
                cert_kuma_id=None, push_kuma_id=None, push_token=None):
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO monitors (chat_id, target, monitor_name, kuma_id, kuma_server,"
            " created_at, cert_kuma_id, push_kuma_id, push_token)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (chat_id, target, monitor_name, kuma_id, kuma_server,
             int(time.time()), cert_kuma_id, push_kuma_id, push_token),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def set_cert_monitor(monitor_id, cert_kuma_id):
    """Привязать SSL-монитор Kuma к записи мониторинга пользователя."""
    conn = connect()
    try:
        conn.execute("UPDATE monitors SET cert_kuma_id=? WHERE id=?", (cert_kuma_id, monitor_id))
        conn.commit()
    finally:
        conn.close()


def set_push_monitor(monitor_id, push_kuma_id, push_token):
    """Привязать push-монитор бэкапов (kuma id + токен пульса) к записи."""
    conn = connect()
    try:
        conn.execute(
            "UPDATE monitors SET push_kuma_id=?, push_token=? WHERE id=?",
            (push_kuma_id, push_token, monitor_id),
        )
        conn.commit()
    finally:
        conn.close()


def list_monitors(chat_id):
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM monitors WHERE chat_id=? ORDER BY id DESC", (chat_id,)
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_monitor(r) for r in rows]


def find_monitor_by_name(monitor_name):
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM monitors WHERE monitor_name=?", (monitor_name,)
        ).fetchone()
    finally:
        conn.close()
    return _row_to_monitor(row)


def update_monitor_status(monitor_name, status):
    conn = connect()
    try:
        conn.execute(
            "UPDATE monitors SET last_status=? WHERE monitor_name=?",
            (status, monitor_name),
        )
        conn.commit()
    finally:
        conn.close()


def remove_monitor(monitor_id):
    conn = connect()
    try:
        conn.execute("DELETE FROM monitors WHERE id=?", (monitor_id,))
        conn.commit()
    finally:
        conn.close()


def find_monitor_by_id(monitor_id):
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM monitors WHERE id=?", (monitor_id,)).fetchone()
    finally:
        conn.close()
    return _row_to_monitor(row)


# ---- Журнал событий (PR-2) ----

EVENT_KEYS = ["id", "chat_id", "kuma_id", "target", "kind", "status",
              "detail", "started_at", "resolved_at"]


def _row_to_event(row):
    if row is None:
        return None
    return dict(zip(EVENT_KEYS, row))


def add_event(chat_id, kuma_id, target, kind, status, detail="",
              started_at=None, resolved_at=None):
    """Записать событие в журнал инцидентов и сроков. Возвращает id записи."""
    now = int(time.time())
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO events (chat_id, kuma_id, target, kind, status, detail,"
            " started_at, resolved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (chat_id, kuma_id, target, kind, status, detail,
             started_at if started_at is not None else now,
             resolved_at),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def close_open_incident(chat_id, target, resolved_at):
    """Закрыть последний открытый инцидент (down без resolved_at) по цели.

    Возвращает число закрытых записей (0 — открытого инцидента не было).
    """
    conn = connect()
    try:
        row = conn.execute(
            "SELECT id FROM events"
            " WHERE chat_id=? AND target=? AND kind='incident'"
            "   AND status='down' AND resolved_at IS NULL"
            " ORDER BY id DESC LIMIT 1",
            (chat_id, target),
        ).fetchone()
        if row is None:
            return 0
        conn.execute("UPDATE events SET resolved_at=? WHERE id=?", (resolved_at, row[0]))
        conn.commit()
        return 1
    finally:
        conn.close()


def list_events(chat_id, since_ts, limit=500):
    """События пользователя с момента since_ts (новые последними)."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM events WHERE chat_id=? AND started_at>=?"
            " ORDER BY id ASC LIMIT ?",
            (chat_id, since_ts, limit),
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_event(r) for r in rows]


# ---- Открытые предупреждения (анти-шум: дедуп порогов) ----

WARNING_KEYS = ["id", "chat_id", "kind", "target", "due_ts", "last_level",
                "last_notified_at", "open", "note"]


def _row_to_warning(row):
    if row is None:
        return None
    return dict(zip(WARNING_KEYS, row))


def upsert_warning(chat_id, kind, target, days=None, note=None):
    """Открыть/обновить предупреждение о сроке.

    days — дней до истечения (известно для ssl/domain); last_level НЕ понижаем
    здесь: его выставляет mark_warning_notified при фактической отправке.
    Если срок «отскочил» вперёд более чем на 60 суток — это продление,
    сбрасываем last_level в 99 (новый цикл, пороги снова активны).
    note — вердикт источника: 'not_found' (домена нет в реестре), ''
    (срок получен, метку снять) или None (не менять прежнее значение).
    Возвращает текущее предупреждение.
    """
    now = int(time.time())
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO warnings (chat_id, kind, target, due_ts, last_level,"
            " last_notified_at, open, note)"
            " VALUES (?, ?, ?, ?, ?, ?, 1, ?)"
            " ON CONFLICT(chat_id, kind, target) DO UPDATE SET"
            "  due_ts=COALESCE(excluded.due_ts, warnings.due_ts),"
            "  last_level=CASE"
            "   WHEN excluded.due_ts IS NOT NULL AND warnings.due_ts IS NOT NULL"
            "        AND excluded.due_ts - warnings.due_ts > 5184000"
            "    THEN 99"                              # продление: новый цикл
            "   ELSE warnings.last_level END,"
            "  note=CASE WHEN excluded.note IS NULL"
            "       THEN warnings.note ELSE excluded.note END,"
            "  open=1",
            (chat_id, kind, target,
             now + days * 86400 if days is not None else None,
             days, now, note),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM warnings WHERE chat_id=? AND kind=? AND target=?",
            (chat_id, kind, target),
        ).fetchone()
        return _row_to_warning(row)
    finally:
        conn.close()


def mark_warning_notified(chat_id, kind, target, level):
    """Отметить, что сообщение по порогу level отправлено (анти-повтор)."""
    conn = connect()
    try:
        conn.execute(
            "UPDATE warnings SET last_level=?, last_notified_at=?"
            " WHERE chat_id=? AND kind=? AND target=?",
            (level, int(time.time()), chat_id, kind, target),
        )
        conn.commit()
    finally:
        conn.close()


def resolve_warning(chat_id, kind, target):
    """Закрыть предупреждение (например, пульс бэкапа снова приходит)."""
    conn = connect()
    try:
        conn.execute(
            "UPDATE warnings SET open=0 WHERE chat_id=? AND kind=? AND target=?",
            (chat_id, kind, target),
        )
        conn.commit()
    finally:
        conn.close()


def list_open_warnings(chat_id):
    """Все открытые предупреждения пользователя (для светофора/отчёта)."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM warnings WHERE chat_id=? AND open=1 ORDER BY id ASC",
            (chat_id,),
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_warning(r) for r in rows]


def get_warning(chat_id, kind, target):
    """Одно предупреждение по цели (для решения «слать или молчать»)."""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM warnings WHERE chat_id=? AND kind=? AND target=?",
            (chat_id, kind, target),
        ).fetchone()
    finally:
        conn.close()
    return _row_to_warning(row)


# ---- Настройки оповещений и планирование (PR-3) ----

PREF_KEYS = ["chat_id", "traffic_light", "light_time", "weekly_report",
             "weekly_day", "weekly_time", "last_light_sent", "last_weekly_sent"]


def get_prefs(chat_id):
    """Настройки пользователя; при первом обращении создаются дефолтные."""
    conn = connect()
    try:
        conn.execute("INSERT OR IGNORE INTO prefs (chat_id) VALUES (?)", (chat_id,))
        conn.commit()
        row = conn.execute("SELECT * FROM prefs WHERE chat_id=?", (chat_id,)).fetchone()
    finally:
        conn.close()
    return dict(zip(PREF_KEYS, row))


def set_prefs(chat_id, **fields):
    """Обновить перечисленные поля настроек (traffic_light, weekly_report и т.п.)."""
    allowed = [k for k in fields if k in PREF_KEYS and k != "chat_id"]
    if not allowed:
        return
    conn = connect()
    try:
        conn.execute("INSERT OR IGNORE INTO prefs (chat_id) VALUES (?)", (chat_id,))
        sets = ", ".join("%s=?" % k for k in allowed)
        conn.execute(
            "UPDATE prefs SET " + sets + " WHERE chat_id=?",
            [fields[k] for k in allowed] + [chat_id],
        )
        conn.commit()
    finally:
        conn.close()


def list_active_users():
    """chat_id пользователей с действующей подпиской/триалом (локальная копия)."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT chat_id FROM users"
            " WHERE status!='canceled' AND paid_until > ?",
            (int(time.time()),),
        ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def mark_digest_sent(chat_id, field, ts):
    """Отметить время отправки светофора/отчёта (идемпотентность дня/недели)."""
    if field not in ("last_light_sent", "last_weekly_sent"):
        return
    conn = connect()
    try:
        conn.execute("UPDATE prefs SET %s=? WHERE chat_id=?" % field, (ts, chat_id))
        conn.commit()
    finally:
        conn.close()


# ---- Домены под контролем (PR-5, domain_watch) ----

DOMAIN_KEYS = ["id", "chat_id", "domain", "due_date", "created_at"]


def _row_to_domain(row):
    if row is None:
        return None
    return dict(zip(DOMAIN_KEYS, row))


def add_domain(chat_id, domain, due_date=None):
    """Взять домен под контроль. False — уже есть (UNIQUE chat_id+domain)."""
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO domains (chat_id, domain, due_date, created_at)"
            " VALUES (?, ?, ?, ?)",
            (chat_id, domain, due_date, int(time.time())),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def remove_domain(chat_id, domain):
    """Убрать домен из контроля. False — его и не было."""
    conn = connect()
    try:
        cur = conn.execute(
            "DELETE FROM domains WHERE chat_id=? AND domain=?", (chat_id, domain)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_domains(chat_id):
    """Домены пользователя, свежие сверху."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM domains WHERE chat_id=? ORDER BY id DESC", (chat_id,)
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_domain(r) for r in rows]


def list_active_domains():
    """(chat_id, domain) всех доменов пользователей с действующей подпиской.

    Для суточного обхода: неактивным не тратим запросы к реестрам.
    """
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT d.chat_id, d.domain FROM domains d"
            " JOIN users u ON u.chat_id = d.chat_id"
            " WHERE u.status!='canceled' AND u.paid_until > ?"
            " ORDER BY d.id ASC",
            (int(time.time()),),
        ).fetchall()
    finally:
        conn.close()
    return [(r[0], r[1]) for r in rows]


def set_domain_due(chat_id, domain, due_date):
    """Запомнить последнюю известную дату окончания регистрации (ISO)."""
    conn = connect()
    try:
        conn.execute(
            "UPDATE domains SET due_date=? WHERE chat_id=? AND domain=?",
            (due_date, chat_id, domain),
        )
        conn.commit()
    finally:
        conn.close()


# ---- Состояние суточных обходов (идемпотентность, как prefs в PR-3) ----


def get_sweep_day(name):
    """Номер МСК-суток последнего обхода (None — ещё не было)."""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT last_day FROM sweep_state WHERE name=?", (name,)
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def mark_sweep_sent(name, day_id):
    """Отметить, что обход name прошёл в сутки day_id (не повторять)."""
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO sweep_state (name, last_day) VALUES (?, ?)"
            " ON CONFLICT(name) DO UPDATE SET last_day=excluded.last_day",
            (name, day_id),
        )
        conn.commit()
    finally:
        conn.close()
