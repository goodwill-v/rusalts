"""SQLite-хранилище состояния Купца: переживает рестарты сервиса.

Заботы в одном файле БД (Config.STATE_DB_PATH):
1. кэш ответов поиска (повтор запроса не тратит кредиты Firecrawl);
2. счётчик израсходованных кредитов по месяцам (для алертов лимитов);
3. журнал отправленных алертов (анти-спам: не слать одно и то же каждый час);
4. квоты поиска по пользователям за календарные сутки (services.quotas);
5. подписки «Жду скидку» и отметки суточного обхода (services.price_watch).

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
    db.execute(
        "CREATE TABLE IF NOT EXISTS quota_usage ("
        " user_id TEXT NOT NULL, day TEXT NOT NULL,"
        " searches INTEGER NOT NULL DEFAULT 0,"
        " PRIMARY KEY (user_id, day))"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS watches ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " user_id TEXT NOT NULL, chat_id TEXT NOT NULL, query TEXT NOT NULL,"
        " created_at REAL NOT NULL,"
        " last_check_at REAL, last_min_price REAL,"
        " active INTEGER NOT NULL DEFAULT 1)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS sweeps ("
        " day TEXT PRIMARY KEY, finished_at REAL NOT NULL)"
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


# ─── Квоты поиска по пользователям (services.quotas) ─────────────────


def quota_searches(user_id: str, day: str, *, path: str | None = None) -> int:
    """Сколько поисков пользователь уже сделал за календарные сутки day."""
    with _connect(path) as db:
        row = db.execute(
            "SELECT searches FROM quota_usage WHERE user_id = ? AND day = ?",
            (user_id, day),
        ).fetchone()
    return int(row[0]) if row else 0


def quota_increment(user_id: str, day: str, *, path: str | None = None) -> int:
    """Списать один поиск: увеличить счётчик и вернуть новое значение."""
    with _LOCK, _connect(path) as db:
        db.execute(
            "INSERT INTO quota_usage (user_id, day, searches) VALUES (?,?,1)"
            " ON CONFLICT(user_id, day) DO UPDATE SET searches = searches + 1",
            (user_id, day),
        )
        row = db.execute(
            "SELECT searches FROM quota_usage WHERE user_id = ? AND day = ?",
            (user_id, day),
        ).fetchone()
    return int(row[0]) if row else 0


# ─── Подписки «Жду скидку» (services.price_watch) ────────────────────

_WATCH_COLS = (
    "id",
    "user_id",
    "chat_id",
    "query",
    "created_at",
    "last_check_at",
    "last_min_price",
    "active",
)


def watch_add(
    user_id: str, chat_id: str, query: str, *, path: str | None = None
) -> int | None:
    """Добавить активную слежку. None — если такая уже активна (анти-дубль,
    без учёта регистра — SQLite lower() не знает кириллицу, сверяем в Python)."""
    with _LOCK, _connect(path) as db:
        rows = db.execute(
            "SELECT query FROM watches WHERE user_id = ? AND active = 1",
            (user_id,),
        ).fetchall()
        if any(r[0].lower() == query.lower() for r in rows):
            return None
        cur = db.execute(
            "INSERT INTO watches (user_id, chat_id, query, created_at)"
            " VALUES (?,?,?,?)",
            (user_id, chat_id, query, time.time()),
        )
        return int(cur.lastrowid)


def _watch_rows(rows) -> list[dict]:
    return [dict(zip(_WATCH_COLS, r)) for r in rows]


def watch_list(
    user_id: str, *, active_only: bool = True, path: str | None = None
) -> list[dict]:
    sql = (
        "SELECT id, user_id, chat_id, query, created_at, last_check_at,"
        " last_min_price, active FROM watches WHERE user_id = ?"
    )
    if active_only:
        sql += " AND active = 1"
    sql += " ORDER BY id"
    with _connect(path) as db:
        rows = db.execute(sql, (user_id,)).fetchall()
    return _watch_rows(rows)


def watch_all_active(*, path: str | None = None) -> list[dict]:
    """Все активные слежки всех пользователей — для суточного обхода."""
    with _connect(path) as db:
        rows = db.execute(
            "SELECT id, user_id, chat_id, query, created_at, last_check_at,"
            " last_min_price, active FROM watches WHERE active = 1 ORDER BY id"
        ).fetchall()
    return _watch_rows(rows)


def watch_count_active(user_id: str, *, path: str | None = None) -> int:
    with _connect(path) as db:
        row = db.execute(
            "SELECT COUNT(*) FROM watches WHERE user_id = ? AND active = 1",
            (user_id,),
        ).fetchone()
    return int(row[0]) if row else 0


def watch_deactivate(
    user_id: str, watch_id: int, *, path: str | None = None
) -> bool:
    """Отключить свою слежку по номеру. False — нет такой активной."""
    with _LOCK, _connect(path) as db:
        cur = db.execute(
            "UPDATE watches SET active = 0"
            " WHERE user_id = ? AND id = ? AND active = 1",
            (user_id, watch_id),
        )
        return cur.rowcount > 0


def watch_mark_checked(
    watch_id: int,
    *,
    min_price: float | None = None,
    checked_at: float | None = None,
    path: str | None = None,
) -> None:
    """Фиксирует результат проверки слежки. min_price=None — цены в выдаче
    не было, прошлая базлайн-цена сохраняется (сравнивать не с чем)."""
    ts = checked_at if checked_at is not None else time.time()
    with _LOCK, _connect(path) as db:
        if min_price is None:
            db.execute(
                "UPDATE watches SET last_check_at = ? WHERE id = ?",
                (ts, watch_id),
            )
        else:
            db.execute(
                "UPDATE watches SET last_check_at = ?, last_min_price = ?"
                " WHERE id = ?",
                (ts, float(min_price), watch_id),
            )


# ─── Отметки суточного обхода слежек ─────────────────────────────────


def sweep_is_done(day: str, *, path: str | None = None) -> bool:
    with _connect(path) as db:
        row = db.execute(
            "SELECT day FROM sweeps WHERE day = ?", (day,)
        ).fetchone()
    return row is not None


def sweep_mark_done(day: str, *, path: str | None = None) -> None:
    with _LOCK, _connect(path) as db:
        db.execute(
            "INSERT OR REPLACE INTO sweeps (day, finished_at) VALUES (?,?)",
            (day, time.time()),
        )
