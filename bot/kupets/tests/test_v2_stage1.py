"""Этап 1 ТЗ v2: поиски/день из планов (6/15/40) + «Разброс цен» в выдаче."""
from __future__ import annotations

import asyncio

import services.quotas as quotas
from bot.messages import format_results
from models.product import ProductData, SellerData
from models.result import ScoreBreakdown, ScoredProduct, SearchFilters, SearchResponse

REAL_PLANS = [
    {"plan": "base", "name": "Базовый", "price_rub": 150, "period_days": 30,
     "trial_days": 7, "resource_limit": 3},
    {"plan": "advanced", "name": "Продвинутый", "price_rub": 500, "period_days": 30,
     "trial_days": 7, "resource_limit": 10},
    {"plan": "marketing", "name": "Маркетинг", "price_rub": 1500, "period_days": 30,
     "trial_days": 7, "resource_limit": 30},
]


class _Sub:
    """Клиент витрины с реальными кодами планов (решение Шефа 05.11)."""

    def __init__(self, plan=""):
        self._plan = plan

    def get_plans(self):
        return REAL_PLANS

    def get_subscription_state(self, user_id):
        if not self._plan:
            return {"present": False}
        return {"present": True, "status": "active", "plan": self._plan}


def test_decision_mapping_constants():
    assert quotas.PLAN_SEARCHES_PER_DAY == {"base": 6, "advanced": 15, "marketing": 40}


def test_plan_searches_base_advanced_marketing(monkeypatch):
    monkeypatch.setattr(
        quotas, "_plans_state", {"at": 1.0, "plans": REAL_PLANS}, raising=False
    )
    for code, searches, watches, name in (
        ("base", 6, 3, "Базовый"),
        ("advanced", 15, 10, "Продвинутый"),
        ("marketing", 40, 30, "Маркетинг"),
    ):
        limits = quotas.plan_limits(code)
        assert limits["searches"] == searches, code
        assert limits["watches"] == watches, code
        assert limits["name"] == name, code


def test_user_plan_resolves_real_codes(monkeypatch):
    monkeypatch.setattr(quotas, "_plans_state", {"at": 0.0, "plans": []})
    code = asyncio.run(quotas.user_plan(1, client=_Sub(plan="advanced")))
    assert code == "advanced"
    assert quotas.plan_limits(code)["searches"] == 15


# ─── Разброс цен ─────────────────────────────────────────────────────


def _sp(name, price, mp, score=70):
    p = ProductData(
        product_id="1", name=name, price=price,
        url=f"https://x.test/{mp}",
        seller=SellerData(seller_id="s", seller_name="Магазин", rating=4.5),
        marketplace=mp,
    )
    return ScoredProduct(
        product=p, score=score, breakdown=ScoreBreakdown()
    )


def _resp(scored):
    r = SearchResponse(filters=SearchFilters(text="термос"))
    r.scored = scored
    r.found_total = len(scored)
    return r


def test_spread_block_shown_for_two_priced_items():
    resp = _resp([
        _sp("Термос A", 2000, "ozon"),
        _sp("Термос B", 1500, "wb"),
    ])
    text = format_results(resp)
    assert "Разброс цен" in text
    assert "1\u202f500 ₽ (Wildberries)" in text
    assert "Дешевле всего — Wildberries" in text


def test_spread_counts_unpriced_items():
    resp = _resp([
        _sp("Термос A", 2000, "ozon"),
        _sp("Термос B", 1500, "wb"),
        _sp("Термос C (цена по ссылке)", 0, "yam"),
    ])
    text = format_results(resp)
    assert "цена по ссылке" in text


def test_no_spread_for_single_priced_item():
    resp = _resp([_sp("Термос A", 2000, "ozon")])
    text = format_results(resp)
    assert "Разброс цен" not in text
