"""SQLite-хранилище состояния Купца: переживает рестарты сервиса.

Три заботы в одном файле БД (Config.STATE_DB_PATH):
1. кэш ответов поиска (повтор запроса не тратит кредиты Firecrawl);
2. счётчик израсходованных кредитов по месяцам (для алертов лимитов);
3. журнал отправленных алертов (анти-спам: не слать одно и то же каждый час).

Только stdlib (sqlite3/pickle) — без внешних зависимостей, тестируется в CI.
"""

from __future__ import annotations

import pickle
import sqlite3
import threading
import time
from pathlib import Path

from config import Config

_LOCK = threading.Lock()


def _connect(path: str | None = None) -> sqlite3.Connection:
    db_path = path or Config.STATE_DB_PATH
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path)
    db.execute(
        "CREATE TABLE IF NOT EXISTS cache ("
        " key TEXT PRIMARY KEY, value BLOB NOT NULL, expires_at REAL NOT NULL)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS credits ("
        " provider TEXT NOT NULL, month TEXT NOT NULL, credits REAL NOT NULL,"
        " PRIMARY KEY (provider, month))"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS alerts ("
        " key TEXT PRIMARY KEY, last_sent_at REAL NOT NULL)"
    )
    return db


# ─── Кэш ответов поиска ──────────────────────────────────────────────


def cache_get(key: str, *, path: str | None = None):
    with _LOCK, _connect(path) as db:
        row = db.execute(
            "SELECT value, expires_at FROM cache WHERE key = ?", (key,)
        ).fetchone()
    if row is None:
        return None
    value, expires_at = row
    if time.time() > expires_at:
        cache_pop(key, path=path)
        return None
    try:
        return pickle.loads(value)
    except Exception:
        return None


def cache_set(key: str, value, ttl_seconds: int, *, path: str | None = None) -> None:
    blob = pickle.dumps(value)
    with _LOCK, _connect(path) as db:
        db.execute(
            "INSERT OR REPLACE INTO cache (key, value, expires_at) VALUES (?,?,?)",
            (key, blob, time.time() + ttl_seconds),
        )


def cache_pop(key: str, *, path: str | None = None) -> None:
    with _LOCK, _connect(path) as db:
        db.execute("DELETE FROM cache WHERE key = ?", (key,))


def cache_clear(*, path: str | None = None) -> None:
    with _LOCK, _connect(path) as db:
        db.execute("DELETE FROM cache")


# ─── Кредиты провайдеров поиска ──────────────────────────────────────


def _current_month() -> str:
    return time.strftime("%Y-%m")


def credits_spent(provider: str, *, path: str | None = None) -> float:
    with _connect(path) as db:
        row = db.execute(
            "SELECT credits FROM credits WHERE provider = ? AND month = ?",
            (provider, _current_month()),
        ).fetchone()
    return float(row[0]) if row else 0.0


def credits_add(provider: str, credits: float, *, path: str | None = None) -> None:
    if credits <= 0:
        return
    with _LOCK, _connect(path) as db:
        db.execute(
            "INSERT INTO credits (provider, month, credits) VALUES (?,?,?)"
            " ON CONFLICT(provider, month) DO UPDATE SET credits = credits + ?",
            (provider, _current_month(), credits, credits),
        )


# ─── Журнал алертов (анти-спам) ──────────────────────────────────────


def alert_last_sent(key: str, *, path: str | None = None) -> float | None:
    with _connect(path) as db:
        row = db.execute(
            "SELECT last_sent_at FROM alerts WHERE key = ?", (key,)
        ).fetchone()
    return float(row[0]) if row else None


def alert_mark_sent(
    key: str, *, sent_at: float | None = None, path: str | None = None
) -> None:
    """sent_at — в той же шкале, что и alert_last_sent (обычно time.monotonic()
    у вызывающего; по умолчанию wall-clock)."""
    with _LOCK, _connect(path) as db:
        db.execute(
            "INSERT OR REPLACE INTO alerts (key, last_sent_at) VALUES (?,?)",
            (key, sent_at if sent_at is not None else time.time()),
        )
