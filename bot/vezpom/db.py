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
  kuma_server   TEXT DEFAULT 'alt'   -- alt | rol
  last_status   TEXT                 -- up | down (last known)
  created_at    INTEGER
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
