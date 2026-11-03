"""Тесты junk-фильтра и весов."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.junk import is_junk_name
from engine.weights import WEIGHTS
from engine.query_parser import extract_filters
from engine.filter import apply_filters
from parser.search import MOCK_PRODUCTS
from models.result import SearchFilters


def test_weights_sum_to_one():
    assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9


def test_junk_tv_box_and_clone():
    assert is_junk_name("Смарт ТВ приставка Android 8/128", smartphone_query=True)
    assert is_junk_name(
        "Смартфон I15 Pro Max в качестве подарка", smartphone_query=True
    )
    assert is_junk_name("Чехол для Samsung Galaxy")
    assert is_junk_name("Xiaomi Redmi Note 14 8/256 ГБ") is None


def test_extract_filters_android_query():
    q = (
        "найди андроид с оперативкой от 8ГБ, встроенной памятью от 128 ГБ "
        "и экраном от 5,5 дюймов до 20000 руб"
    )
    f = extract_filters(q)
    assert f.price_max == 20000
    assert f.ram_gb == 8
    assert f.storage_gb == 128
    assert f.screen_inches == 5.5
    assert f.text.strip() == "андроид"


def test_filter_rejects_fake_and_low_ram():
    filters = SearchFilters(
        text="андроид",
        price_max=20000,
        ram_gb=8,
        storage_gb=128,
        screen_inches=5.5,
    )
    buckets = apply_filters(list(MOCK_PRODUCTS), filters, smartphone_query=True)
    rejected_names = {p.name for p, _ in buckets.rejected}
    assert any("Noname" in n for n in rejected_names)
    assert any("Realme" in n for n in rejected_names)
    near_names = {p.name for p, _ in buckets.near_miss}
    assert any("BlackBox" in n or "спецификаций" in n for n in near_names)
    assert len(buckets.passed) >= 1
