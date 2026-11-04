"""Этап 2 ТЗ v2: квоты фич, парсер сравнения, анти-клон вердикты, формат."""
from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import services.anticlone as anticlone
import services.compare as cmp
import services.quotas as quotas
import services.sqlite_store as store
from bot.messages import check_result_message, compare_result_message
from models.product import SearchItem

MSK = ZoneInfo("Europe/Moscow")
DAY = datetime(2026, 11, 5, 12, 0, tzinfo=MSK)


# ─── Квоты фич ───────────────────────────────────────────────────────


def _tmp(monkeypatch, tmp_path):
    db = str(tmp_path / "state.db")
    monkeypatch.setattr(store.Config, "STATE_DB_PATH", db, raising=False)
    return db


def test_feature_limits_matrix():
    # матрица ТЗ §3.0: xvsy 0/10/40/150; check 1(день)/20/80/безлимит
    assert quotas.feature_limit("free", "xvsy") == (0, "month")
    assert quotas.feature_limit("base", "xvsy") == (10, "month")
    assert quotas.feature_limit("advanced", "xvsy") == (40, "month")
    assert quotas.feature_limit("marketing", "xvsy") == (150, "month")
    assert quotas.feature_limit("free", "check") == (1, "day")
    assert quotas.feature_limit("marketing", "check") == (None, "month")
    assert quotas.feature_limit("unknown-plan", "xvsy") == (0, "month")  # консервативно


def test_check_feature_daily_for_free(monkeypatch, tmp_path):
    _tmp(monkeypatch, tmp_path)
    ok, info = quotas.check_feature_quota(1, "free", "check", now=DAY)
    assert ok and info["limit"] == 1 and info["period"] == "day"
    quotas.consume_feature(1, "check", "free", now=DAY)
    ok, info = quotas.check_feature_quota(1, "free", "check", now=DAY)
    assert not ok  # вторая проверка за день free — нельзя


def test_xvsy_monthly_counter(monkeypatch, tmp_path):
    _tmp(monkeypatch, tmp_path)
    for i in range(10):
        ok, _ = quotas.check_feature_quota(2, "base", "xvsy", now=DAY)
        assert ok
        quotas.consume_feature(2, "xvsy", "base", now=DAY)
    ok, info = quotas.check_feature_quota(2, "base", "xvsy", now=DAY)
    assert not ok and info["used"] == 10 and info["limit"] == 10
    # другой пользователь — свой счётчик
    ok, _ = quotas.check_feature_quota(3, "base", "xvsy", now=DAY)
    assert ok


def test_marketing_check_unlimited(monkeypatch, tmp_path):
    _tmp(monkeypatch, tmp_path)
    quotas.consume_feature(4, "check", "marketing", now=DAY)
    quotas.consume_feature(4, "check", "marketing", now=DAY)
    ok, info = quotas.check_feature_quota(4, "marketing", "check", now=DAY)
    assert ok and info["limit"] is None


# ─── Парсер сравнения ────────────────────────────────────────────────


def test_parse_compare():
    assert cmp.parse_compare_query("сравни Redmi Note 14 и Realme C75") == (
        "Redmi Note 14", "Realme C75"
    )
    assert cmp.parse_compare_query("сравни iPhone 15 vs Galaxy S24") == (
        "iPhone 15", "Galaxy S24"
    )
    assert cmp.parse_compare_query("сравнить термопот и чайник") == ("термопот", "чайник")
    assert cmp.parse_compare_query("сравни") is None
    assert cmp.parse_compare_query("куплю iPhone 15") is None  # не «сравни»


def _item(name, price, rating, reviews, mp="ozon"):
    return SearchItem(
        product_id="1", name=name, price=price, rating=rating,
        reviews_count=reviews, url=f"https://x/{mp}", marketplace=mp,
    )


def test_pick_best_prefers_priced_and_rated():
    items = [
        _item("Без цены", 0, 4.5, 100),
        _item("С ценой", 2000, 4.2, 300),
    ]
    assert cmp.pick_best(items).name == "С ценой"


def test_compare_verdict_price_gap():
    a = _item("Дешёвый", 2000, 4.3, 200)
    b = _item("Дорогой", 2600, 4.3, 200)
    verdict = cmp.compare_verdict(a, b)
    assert "Дешевле" in verdict and "23%" in verdict
    assert "переформулируйте" in cmp.compare_verdict(None, None)


# ─── Анти-клон ───────────────────────────────────────────────────────


def test_clone_name_is_danger():
    v = anticlone.verdict_for("I15 Pro Max 256гб новый", [])
    assert v["level"] == "danger" and v["confidence"] == "высокая"


def test_normal_name_is_ok():
    v = anticlone.verdict_for("термос для школы 1 литр", [])
    assert v["level"] == "ok"


def test_suspicious_price_is_warn():
    items = [
        _item("iPhone 15 128", 9000, 4.5, 500, "ozon"),
        _item("iPhone 15 128", 31000, 4.6, 800, "wb"),
        _item("iPhone 15 128", 30500, 4.4, 300, "yam"),
    ]
    v = anticlone.verdict_for("iPhone 15 128гб", items)
    assert v["level"] == "warn"
    assert any("30% медианы" in r for r in v["reasons"])


# ─── Формат сообщений ────────────────────────────────────────────────


def test_compare_message_format():
    a = {"name": "Redmi Note 14", "price": 17000, "rating": 4.3,
         "reviews_count": 612, "marketplace": "ozon", "url": "https://oz"}
    b = {"name": "Realme C75", "price": 0, "rating": None,
         "reviews_count": 0, "marketplace": "wb", "url": "https://wb"}
    text = compare_result_message("Redmi", "Realme", a, b, "A дешевле.", "минус 2 поиска")
    assert "Сравнение" in text and "цена по ссылке" in text and "минус 2 поиска" in text
    empty = compare_result_message("X", "Y", None, None, "ничего", "")
    assert "не нашёл" in empty


def test_check_message_format():
    text = check_result_message("I15 Pro Max", {
        "level": "danger", "confidence": "высокая", "reasons": ["похоже на клон"],
    })
    assert "высокая вероятность" in text and "похоже на клон" in text
