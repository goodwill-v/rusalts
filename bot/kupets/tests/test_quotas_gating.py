"""Гейтинг тарифов Купца: resource_limit из /api/tariffs/kupec (PR #71/#75 style).

Фолбэки как у ВежПом: планы не заданы → константы (5); код плана неизвестен →
первый план; сетевой сбой витрины → fail-open платный; 404 → free.
"""
from __future__ import annotations

import asyncio

import services.quotas as quotas
import services.sqlite_store as store

PLANS = [
    {"plan": "base", "name": "Базовый", "price_rub": 150, "period_days": 30,
     "trial_days": 7, "resource_limit": 3},
    {"plan": "pro", "name": "Продвинутый", "price_rub": 500, "period_days": 30,
     "trial_days": 7, "resource_limit": 10},
    {"plan": "max", "name": "Максимум", "price_rub": 1500, "period_days": 30,
     "trial_days": 7, "resource_limit": 30},
]


class FakeClient:
    """Полный контракт гейтинга: планы + трёхсостоятельная подписка."""

    def __init__(self, plans=PLANS, present=None, status="active", plan=""):
        self._plans = plans
        self._present = present
        self._status = status
        self._plan = plan

    def get_plans(self):
        return self._plans

    def get_subscription_state(self, user_id):
        if self._present is None:
            return {"present": None}          # сетевой сбой витрины
        if not self._present:
            return {"present": False}         # 404 — подписки нет
        return {"present": True, "status": self._status, "plan": self._plan}


def _reset(monkeypatch, tmp_path):
    db = str(tmp_path / "state.db")
    monkeypatch.setattr(store.Config, "STATE_DB_PATH", db, raising=False)
    monkeypatch.setattr(quotas, "_plans_state", {"at": 0.0, "plans": []})


def _plan_of(monkeypatch, client):
    return asyncio.run(quotas.user_plan(777, client=client))


def test_active_plan_maps_to_its_resource_limit(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)
    code = _plan_of(monkeypatch, FakeClient(plan="pro", present=True))
    assert code == "pro"
    assert quotas.plan_limits("pro")["watches"] == 10
    assert quotas.plan_limits("pro")["searches"] == 6
    assert quotas.plan_limits("pro")["name"] == "Продвинутый"


def test_unknown_plan_code_falls_back_to_first_plan(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)
    code = _plan_of(monkeypatch, FakeClient(plan="ghost", present=True))
    assert code == "base"  # первый план продукта, как у ВежПом


def test_no_subscription_is_free(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)
    code = _plan_of(monkeypatch, FakeClient(present=False))
    assert code == quotas.FREE_PLAN
    assert quotas.plan_limits(code)["watches"] == 0
    assert quotas.plan_limits(code)["searches"] == 3


def test_expired_subscription_is_free(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)
    code = _plan_of(monkeypatch, FakeClient(present=True, status="expired", plan="pro"))
    assert code == quotas.FREE_PLAN


def test_network_fail_failopen_gives_base_paid_plan(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)
    code = _plan_of(monkeypatch, FakeClient(present=None))
    assert code == quotas.KUPEC_PLAN
    # при засеянных планах fail-open → базовый платный план, а не free
    assert quotas.plan_limits(code)["watches"] == 3
    assert quotas.plan_limits(code)["searches"] == 6


def test_no_plans_seeded_legacy_constants(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)
    code = _plan_of(monkeypatch, FakeClient(plans=[], present=True, plan=""))
    assert code == quotas.KUPEC_PLAN
    assert quotas.plan_limits(code)["watches"] == 5   # прежний хардкод
    assert quotas.plan_limits(code)["searches"] == 6


def test_unknown_code_is_conservatively_free():
    assert quotas.plan_limits("nope") == quotas.plan_limits(quotas.FREE_PLAN)


def test_watch_quota_uses_plan_limit(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)
    db = str(tmp_path / "state.db")
    monkeypatch.setattr(store.Config, "STATE_DB_PATH", db, raising=False)
    monkeypatch.setattr(quotas, "_plans_state", {"at": 1.0, "plans": PLANS})
    # 9 активных слежек при лимите pro=10 — можно ещё одну
    monkeypatch.setattr(store, "watch_count_active", lambda *a, **k: 9)
    ok, info = quotas.check_watch_quota(777, "pro")
    assert ok and info["limit"] == 10 and info["plan_name"] == "Продвинутый"
    monkeypatch.setattr(store, "watch_count_active", lambda *a, **k: 10)
    ok, info = quotas.check_watch_quota(777, "pro")
    assert not ok


def test_legacy_client_without_plan_methods(monkeypatch, tmp_path):
    """Старый duck-typing клиент (только check_subscription) — прежние константы."""

    class Legacy:
        def check_subscription(self, user_id):
            return True

    _reset(monkeypatch, tmp_path)
    code = _plan_of(monkeypatch, Legacy())
    assert code == quotas.KUPEC_PLAN
    assert quotas.plan_limits(code)["watches"] == 5
