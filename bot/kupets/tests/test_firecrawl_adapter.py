"""Адаптер Firecrawl: товары без цены не теряются (баг «Ничего не нашёл»)."""
from __future__ import annotations

from parser.firecrawl_search import (
    _extract_product_id,
    _marketplace_of,
    _parse_results,
)


def _web(*, url, title="Термос детский 1л", desc="Хорошее качество"):
    return {"url": url, "title": title, "description": desc}


def test_items_without_price_are_kept():
    """Раньше фильтр цены оставлял 0 из 36: у Ozon цены в сниппетах нет."""
    data = {"web": [
        _web(url="https://www.ozon.ru/product/termos-1797628370/"),
        _web(url="https://www.ozon.ru/product/drugoy-123456789/"),
    ]}
    items = _parse_results(data)
    assert len(items) == 2
    assert all(it.price == 0.0 for it in items)  # «цена по ссылке»
    assert all(it.marketplace == "ozon" for it in items)


def test_price_parsed_when_present():
    rub = chr(8381)
    data = {"web": [
        _web(
            url="https://www.wildberries.ru/catalog/245678901/detail.aspx",
            desc=f"Термос для школы, цена 1 350 {rub}",
        ),
    ]}
    items = _parse_results(data)
    assert len(items) == 1
    assert items[0].price == 1350.0
    assert items[0].marketplace == "wb"


def test_category_pages_are_skipped():
    data = {"web": [
        _web(url="https://www.ozon.ru/category/termosy-15687/", title="Купить на OZON"),
        _web(url="https://www.ozon.ru/highlight/termosy/", title="Подборка"),
    ]}
    assert _parse_results(data) == []


def test_three_marketplaces_recognized():
    assert _marketplace_of("https://www.ozon.ru/product/x-1/") == "ozon"
    assert _marketplace_of("https://www.wildberries.ru/catalog/1/detail.aspx") == "wb"
    assert _marketplace_of("https://market.yandex.ru/product--termos/123456/") == "yam"
    assert _marketplace_of("https://example.com/") is None


def test_product_ids_extracted():
    assert _extract_product_id("https://www.ozon.ru/product/termos-1797628370/") == "1797628370"
    assert _extract_product_id("https://www.wildberries.ru/catalog/245678901/detail.aspx") == "245678901"
    assert _extract_product_id("https://market.yandex.ru/product--termos-detskiy/54321987/") == "54321987"
