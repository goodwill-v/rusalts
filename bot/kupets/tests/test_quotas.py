"""Квоты тарифов: free/«Покупатель»/триал, исчерпание, сброс по суткам."""
from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import services.quotas as quotas
import services.sqlite_store as store

MOSCOW = ZoneInfo("Europe/Moscow")

DAY1 = datetime(2026, 11, 4, 12, 0, tzinfo=MOSCOW)
DAY2 = datetime(2026, 11, 5, 12, 0, tzinfo=MOSCOW)


def _tmp(monkeypatch, tmp_path):
    db = str(tmp_path / "state.db")
    monkeypatch.setattr(store.Config, "STATE_DB_PATH", db, raising=False)
    return db


class _FakeVitrina:
    """Утиная типизация VitrinaClient.check_subscription (sync-контракт)."""

    def __init__(self, active: bool = True, fail: bool = False):
        self.active = active
        self.fail = fail

    def check_subscription(self, user_id) -> bool:
        if self.fail:
            raise RuntimeError("vitrina down")
        return self.active


def test_plan_resolution():
    assert quotas.resolve_plan(True) == quotas.KUPEC_PLAN
    assert quotas.resolve_plan(False) == quotas.FREE_PLAN
    assert quotas.plan_limits(quotas.FREE_PLAN)["searches"] == 3
    assert quotas.plan_limits(quotas.FREE_PLAN)["watches"] == 0
    assert quotas.plan_limits(quotas.KUPEC_PLAN)["searches"] == 6
    assert quotas.plan_limits(quotas.KUPEC_PLAN)["watches"] == 5
    # неизвестный тариф — консервативно как free
    assert quotas.plan_limits("nope") == quotas.plan_limits(quotas.FREE_PLAN)


def test_quota_day_is_msk_calendar_day():
    # 23:30 UTC 4 ноября = 02:30 МСК 5 ноября — сутки уже другие
    late_utc = datetime(2026, 11, 4, 23, 30, tzinfo=ZoneInfo("UTC"))
    assert quotas.quota_day(late_utc) == "2026-11-05"
    assert quotas.quota_day(DAY1) == "2026-11-04"


def test_free_quota_three_searches_then_denied(monkeypatch, tmp_path):
    _tmp(monkeypatch, tmp_path)
    plan = quotas.FREE_PLAN
    for _ in range(quotas.FREE_SEARCHES_PER_DAY):
        ok, info = quotas.check_search_quota(101, plan, now=DAY1)
        assert ok
        quotas.consume_search(101, plan, now=DAY1)
    ok, info = quotas.check_search_quota(101, plan, now=DAY1)
    assert not ok
    assert info["used"] == 3
    assert info["limit"] == 3


def test_kupec_quota_six_searches_then_denied(monkeypatch, tmp_path):
    _tmp(monkeypatch, tmp_path)
    plan = quotas.KUPEC_PLAN
    for _ in range(quotas.KUPEC_SEARCHES_PER_DAY):
        ok, _ = quotas.check_search_quota(202, plan, now=DAY1)
        assert ok
        quotas.consume_search(202, plan, now=DAY1)
    ok, info = quotas.check_search_quota(202, plan, now=DAY1)
    assert not ok
    assert info["used"] == 6
    assert info["limit"] == 6


def test_trial_gets_kupec_quota(monkeypatch, tmp_path):
    """Триал на витрине — тот же status=active, лимиты «Покупателя»."""
    _tmp(monkeypatch, tmp_path)
    plan = asyncio.run(
        quotas.user_plan(303, client=_FakeVitrina(active=True))
    )
    assert plan == quotas.KUPEC_PLAN
    ok, info = quotas.check_search_quota(303, plan, now=DAY1)
    assert ok
    assert info["limit"] == quotas.KUPEC_SEARCHES_PER_DAY


def test_paid_user_resolved_from_vitrina(monkeypatch, tmp_path):
    _tmp(monkeypatch, tmp_path)
    assert (
        asyncio.run(quotas.user_plan(404, client=_FakeVitrina(active=True)))
        == quotas.KUPEC_PLAN
    )
    assert (
        asyncio.run(quotas.user_plan(405, client=_FakeVitrina(active=False)))
        == quotas.FREE_PLAN
    )


def test_vitrina_fail_open_to_kupec(monkeypatch, tmp_path):
    """Сбой витрины не наказывает пользователя (fail-open контракта)."""
    _tmp(monkeypatch, tmp_path)
    plan = asyncio.run(
        quotas.user_plan(505, client=_FakeVitrina(fail=True))
    )
    assert plan == quotas.KUPEC_PLAN


def test_quota_resets_next_calendar_day(monkeypatch, tmp_path):
    _tmp(monkeypatch, tmp_path)
    for _ in range(quotas.FREE_SEARCHES_PER_DAY):
        quotas.consume_search(606, quotas.FREE_PLAN, now=DAY1)
    ok, _ = quotas.check_search_quota(606, quotas.FREE_PLAN, now=DAY1)
    assert not ok  # квота за 4.11 исчерпана
    ok, info = quotas.check_search_quota(606, quotas.FREE_PLAN, now=DAY2)
    assert ok
    assert info["used"] == 0


def test_watch_quota_free_denied_kupec_five(monkeypatch, tmp_path):
    _tmp(monkeypatch, tmp_path)
    # free: слежек 0, лимит 0 — нельзя ни одной
    ok, info = quotas.check_watch_quota(707, quotas.FREE_PLAN)
    assert not ok
    assert info["limit"] == 0

    # kupec: 5 можно, 6-я нет
    for i in range(quotas.KUPEC_WATCH_LIMIT):
        ok, info = quotas.check_watch_quota(708, quotas.KUPEC_PLAN)
        assert ok
        assert store.watch_add("708", "42", f"термос {i}") is not None
    ok, info = quotas.check_watch_quota(708, quotas.KUPEC_PLAN)
    assert not ok
    assert info["count"] == 5
    assert info["limit"] == 5
