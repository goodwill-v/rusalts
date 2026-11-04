"""Слежка «Жду скидку»: хранение, детект цены, суточный обход, окно."""
from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import services.price_watch as pw
import services.sqlite_store as store

MOSCOW = ZoneInfo("Europe/Moscow")

# утро внутри окна обхода (07:00–09:00 МСК)
DAY1 = datetime(2026, 11, 4, 8, 0, tzinfo=MOSCOW)
DAY2 = datetime(2026, 11, 5, 8, 0, tzinfo=MOSCOW)
DAY3 = datetime(2026, 11, 6, 8, 0, tzinfo=MOSCOW)


def _tmp(monkeypatch, tmp_path):
    db = str(tmp_path / "state.db")
    monkeypatch.setattr(store.Config, "STATE_DB_PATH", db, raising=False)
    return db


def _product(price, name="Термос 1л", url="https://ozon.ru/t/1"):
    return SimpleNamespace(
        product=SimpleNamespace(price=price, name=name, url=url)
    )


def _response(scored, provider_limited=False):
    return SimpleNamespace(scored=scored, provider_limited=provider_limited)


def _notify_sink(chats):
    async def notify(chat_id, text):
        chats.append((chat_id, text))

    return notify


def _search_queue(responses, calls):
    async def search_fn(query):
        calls.append(query)
        return responses.pop(0)

    return search_fn


def _paid_plan(monkeypatch):
    """Убирает сетевой вызов витрины из обхода: все пользователи «Покупатель»."""

    async def _kupec(user_id, client=None):
        return "kupec"

    import services.quotas as quotas

    monkeypatch.setattr(quotas, "user_plan", _kupec)


# ─── Хранение подписок ───────────────────────────────────────────────


def test_watch_add_list_deactivate(monkeypatch, tmp_path):
    _tmp(monkeypatch, tmp_path)
    wid = store.watch_add("u1", "42", "термос для школы")
    assert wid is not None
    watches = store.watch_list("u1")
    assert len(watches) == 1
    assert watches[0]["query"] == "термос для школы"
    assert watches[0]["chat_id"] == "42"
    assert watches[0]["active"] == 1
    assert watches[0]["last_min_price"] is None
    assert watches[0]["last_check_at"] is None

    assert store.watch_deactivate("u1", wid) is True
    assert store.watch_list("u1") == []
    assert store.watch_deactivate("u1", wid) is False  # уже отключена
    history = store.watch_list("u1", active_only=False)
    assert history[0]["active"] == 0


def test_watch_duplicate_same_query(monkeypatch, tmp_path):
    _tmp(monkeypatch, tmp_path)
    assert store.watch_add("u1", "42", "термос") is not None
    # повтор (в другом регистре) — дубль, не добавляем
    assert store.watch_add("u1", "42", "Термос") is None
    # другой пользователь — можно
    assert store.watch_add("u2", "43", "термос") is not None
    assert len(store.watch_list("u1")) == 1
    assert len(store.watch_all_active()) == 2


def test_watch_mark_checked_keeps_baseline_on_empty(monkeypatch, tmp_path):
    _tmp(monkeypatch, tmp_path)
    wid = store.watch_add("u1", "42", "термос")
    store.watch_mark_checked(wid, min_price=1890.0)
    assert store.watch_list("u1")[0]["last_min_price"] == 1890.0
    # выдача без цен — прошлая базлайн-цена сохраняется
    store.watch_mark_checked(wid, min_price=None)
    assert store.watch_list("u1")[0]["last_min_price"] == 1890.0
    assert store.watch_list("u1")[0]["last_check_at"] is not None


# ─── Детект изменения цены ───────────────────────────────────────────


def test_detect_price_change_cases():
    # первая проверка — базлайн, молчим
    assert pw.detect_price_change(None, 100) == (False, None)
    assert pw.detect_price_change(None, None) == (False, None)
    # было «цена по ссылке» (0) → появилось число — сообщаем
    assert pw.detect_price_change(0, 100) == (True, "appeared")
    # падение и рост — сообщаем
    assert pw.detect_price_change(100, 90) == (True, "down")
    assert pw.detect_price_change(100, 120) == (True, "up")
    # без изменений и без цен — молчим
    assert pw.detect_price_change(100, 100) == (False, None)
    assert pw.detect_price_change(100, None) == (False, None)
    assert pw.detect_price_change(100, 0) == (False, None)


def test_apply_check_result_baseline_rules():
    # старт: цен нет ни разу — пишем базлайн 0 («цен нет»)
    assert pw.apply_check_result(None, None) == (False, None, 0.0)
    # старт: цена нашлась — пишем её, молча
    assert pw.apply_check_result(None, 100) == (False, None, 100.0)
    # была цена, выдача пустая — базлайн сохраняем
    assert pw.apply_check_result(100.0, None) == (False, None, None)
    # изменение — пишем новое значение
    assert pw.apply_check_result(100.0, 90.0) == (True, "down", 90.0)


def test_min_price_from_response():
    assert pw.min_price_from_response(
        _response([_product(150), _product(0), _product(90)])
    ) == 90
    assert pw.min_price_from_response(_response([])) is None
    assert pw.min_price_from_response(_response([_product(0)])) is None
    assert pw.min_price_from_response(_response([_product(120)])) == 120


def test_cheapest_product_picked():
    best = pw.cheapest_product_from_response(
        _response([_product(150, name="Дорогой"), _product(90, name="Дешёвый")])
    )
    assert best["name"] == "Дешёвый"
    assert best["url"] == "https://ozon.ru/t/1"


def test_format_watch_alert_variants():
    down = pw.format_watch_alert("термос", 100.0, 90.0, None)
    assert "100" in down and "90 ₽" in down and "скидка 10%" in down
    up = pw.format_watch_alert("термос", 100.0, 110.0, None)
    assert "повышение 10%" in up
    appeared = pw.format_watch_alert("термос", 0, 500.0, None)
    assert "цены в выдаче не было" in appeared and "500 ₽" in appeared
    # markdown-опасные символы в запросе экранируются
    safe = pw.format_watch_alert("тест_с *звёзд*", 100.0, 90.0, None)
    assert "тест_с" not in safe


# ─── Суточный обход ──────────────────────────────────────────────────


def test_sweep_baseline_then_change_notifies(monkeypatch, tmp_path):
    _paid_plan(monkeypatch)
    db = _tmp(monkeypatch, tmp_path)
    wid = store.watch_add("u1", "42", "термос", path=db)
    calls, responses = [], []

    # день 1: базлайн (мин. цена 100) — молча
    responses = [_response([_product(120), _product(100)])]
    chats = []
    res = asyncio.run(
        pw.sweep_once(
            _search_queue(responses, calls), _notify_sink(chats), now=DAY1, path=db
        )
    )
    assert res["status"] == "done" and res["checked"] == 1
    assert res["notified"] == 0
    assert chats == []
    w = store.watch_list("u1", path=db)[0]
    assert w["last_min_price"] == 100
    assert w["last_check_at"] is not None

    # день 2: цена не изменилась — молчание (анти-шум)
    responses = [_response([_product(100)])]
    res = asyncio.run(
        pw.sweep_once(
            _search_queue(responses, calls), _notify_sink(chats), now=DAY2, path=db
        )
    )
    assert res["notified"] == 0 and chats == []

    # день 3: падение до 90 — сообщение с «было → стало»
    responses = [_response([_product(95), _product(90, url="https://ozon.ru/x")])]
    res = asyncio.run(
        pw.sweep_once(
            _search_queue(responses, calls), _notify_sink(chats), now=DAY3, path=db
        )
    )
    assert res["notified"] == 1
    assert len(chats) == 1
    chat_id, text = chats[0]
    assert chat_id == "42"
    assert "100" in text and "90 ₽" in text
    assert "скидка" in text
    assert "https://ozon.ru/x" in text  # ссылка на самый дешёвый
    assert store.watch_list("u1", path=db)[0]["last_min_price"] == 90


def test_sweep_appeared_after_empty(monkeypatch, tmp_path):
    _paid_plan(monkeypatch)
    db = _tmp(monkeypatch, tmp_path)
    store.watch_add("u1", "42", " iphone 15 128гб", path=db)
    chats = []
    # день 1: цен в выдаче нет → базлайн 0
    res = asyncio.run(
        pw.sweep_once(
            _search_queue([_response([_product(0)])], []),
            _notify_sink(chats),
            now=DAY1,
            path=db,
        )
    )
    assert res["notified"] == 0
    assert store.watch_list("u1", path=db)[0]["last_min_price"] == 0
    # день 2: появилась цена → сообщаем
    res = asyncio.run(
        pw.sweep_once(
            _search_queue([_response([_product(54_990)])], []),
            _notify_sink(chats),
            now=DAY2,
            path=db,
        )
    )
    assert res["notified"] == 1
    assert "54 990".replace(" ", "\u202f") in chats[0][1] or "54" in chats[0][1]


def test_sweep_marks_day_and_skips_second_run(monkeypatch, tmp_path):
    _paid_plan(monkeypatch)
    db = _tmp(monkeypatch, tmp_path)
    store.watch_add("u1", "42", "термос", path=db)
    calls = []
    res = asyncio.run(
        pw.sweep_once(
            _search_queue([_response([_product(100)])], calls),
            _notify_sink([]),
            now=DAY1,
            path=db,
        )
    )
    assert res["status"] == "done"
    assert store.sweep_is_done("2026-11-04", path=db)
    # повторный заход в то же сутки — ничего не делает
    res = asyncio.run(
        pw.sweep_once(
            _search_queue([], calls), _notify_sink([]), now=DAY1, path=db
        )
    )
    assert res["status"] == "already_done"
    assert len(calls) == 1  # поиск был только один


def test_sweep_interrupted_by_provider_limit(monkeypatch, tmp_path):
    _paid_plan(monkeypatch)
    db = _tmp(monkeypatch, tmp_path)
    store.watch_add("u1", "42", "термос", path=db)
    chats = []
    limited = _response([], provider_limited=True)
    res = asyncio.run(
        pw.sweep_once(
            _search_queue([limited], []), _notify_sink(chats), now=DAY1, path=db
        )
    )
    assert res["status"] == "interrupted_credits"
    assert chats == []
    assert not store.sweep_is_done("2026-11-04", path=db)  # вернёмся в окне
    assert store.watch_list("u1", path=db)[0]["last_check_at"] is None


def test_sweep_skipped_when_credits_exhausted(monkeypatch, tmp_path):
    db = _tmp(monkeypatch, tmp_path)
    store.watch_add("u1", "42", "термос", path=db)
    calls = []

    def _exhausted():
        return {"level": "exhausted", "spent": 1000, "limit": 1000, "pct": 100}

    import services.credit_monitor as cm

    monkeypatch.setattr(cm, "usage_snapshot", _exhausted)
    chats = []
    res = asyncio.run(
        pw.sweep_once(
            _search_queue([], calls), _notify_sink(chats), now=DAY1, path=db
        )
    )
    assert res["status"] == "skipped_credits"
    assert calls == [] and chats == []
    assert not store.sweep_is_done("2026-11-04", path=db)


def test_sweep_skips_free_plan_users(monkeypatch, tmp_path):
    """free = «БЕЗ слежки»: слежка остаётся активной, но обход пропускает."""
    db = _tmp(monkeypatch, tmp_path)
    store.watch_add("u1", "42", "термос", path=db)
    calls = []

    async def _free_plan(user_id, client=None):
        return "free"

    import services.quotas as quotas

    monkeypatch.setattr(quotas, "user_plan", _free_plan)
    chats = []
    res = asyncio.run(
        pw.sweep_once(
            _search_queue([_response([_product(100)])], calls),
            _notify_sink(chats),
            now=DAY1,
            path=db,
        )
    )
    assert res["status"] == "done"
    assert res["checked"] == 0
    assert res["skipped_free"] == 1
    assert calls == [] and chats == []
    # слежка не удалена — возобновится при возврате подписки
    assert store.watch_list("u1", path=db)[0]["active"] == 1


def test_sweep_search_error_does_not_kill_cycle(monkeypatch, tmp_path):
    _paid_plan(monkeypatch)
    db = _tmp(monkeypatch, tmp_path)
    store.watch_add("u1", "42", "термос", path=db)
    store.watch_add("u1", "42", "ноутбук", path=db)

    async def bad_then_good(query):
        if query == "термос":
            raise RuntimeError("network down")
        return _response([_product(200)])

    chats = []
    res = asyncio.run(
        pw.sweep_once(bad_then_good, _notify_sink(chats), now=DAY1, path=db)
    )
    assert res["status"] == "done"
    assert res["errors"] == 1
    assert res["checked"] == 1  # вторая слежка всё равно проверена


# ─── Окно планировщика ───────────────────────────────────────────────


def test_sweep_window_hours():
    assert pw.in_sweep_window(datetime(2026, 11, 4, 7, 0, tzinfo=MOSCOW))
    assert pw.in_sweep_window(datetime(2026, 11, 4, 8, 59, tzinfo=MOSCOW))
    assert not pw.in_sweep_window(datetime(2026, 11, 4, 6, 59, tzinfo=MOSCOW))
    assert not pw.in_sweep_window(datetime(2026, 11, 4, 9, 0, tzinfo=MOSCOW))
    assert not pw.in_sweep_window(datetime(2026, 11, 4, 23, 30, tzinfo=MOSCOW))
