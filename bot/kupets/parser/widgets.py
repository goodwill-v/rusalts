"""Парсинг widgetStates из ответа Composer API."""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

from models.product import ProductData, SearchItem, SellerData
from parser.specs import extract_specs_from_title
from utils import get_logger

logger = get_logger("parser.widgets")


def extract_search_items(payload: dict[str, Any]) -> list[SearchItem]:
    widget = _find_product_grid(payload.get("widgetStates") or {})
    if not widget:
        logger.warning("no_product_grid_widget")
        return []

    items_raw = list(_walk_tile_items(widget))
    results: list[SearchItem] = []
    seen: set[str] = set()
    for raw in items_raw:
        item = _tile_to_search_item(raw)
        if item and item.product_id not in seen:
            seen.add(item.product_id)
            results.append(item)
    return results


def search_item_to_product(item: SearchItem) -> ProductData:
    """Обогащение уровня 1→2 без карточки: specs из названия + доставка из плитки."""
    specs = extract_specs_from_title(item.name)
    delivery_days, delivery_type = _parse_delivery(item)
    seller = SellerData(
        seller_id=item.seller_id or "unknown",
        seller_name=item.seller_name or "Продавец Ozon",
        rating=None,
        orders_count=None,
        is_verified=item.has_fbo,
    )
    product = ProductData(
        product_id=item.product_id,
        name=item.name,
        price=item.price,
        old_price=item.old_price,
        rating=item.rating,
        reviews_count=item.reviews_count or 0,
        seller_name=seller.seller_name,
        seller_id=seller.seller_id,
        seller_url="",
        seller=seller,
        delivery_type=delivery_type,
        delivery_days=delivery_days,
        delivery_price=0.0,
        ram_gb=specs.ram_gb,
        storage_gb=specs.storage_gb,
        screen_inches=specs.screen_inches,
        specs_confirmed=specs.confirmed,
        url=item.url,
        photos=[],
        marketplace=item.marketplace,
    )
    enrich_delivery_from_tile(product, item.delivery_text)
    return product


def merge_product_payload(
    item: SearchItem,
    payload: dict[str, Any],
) -> ProductData:
    """Если карточка открылась — дополняем продавца и характеристики."""
    base = search_item_to_product(item)
    ws = payload.get("widgetStates") or {}
    attrs = _extract_characteristics(ws)
    if attrs:
        from parser.specs import (
            extract_specs_from_attributes,
            merge_specs,
            extract_specs_from_title,
        )

        specs = merge_specs(
            extract_specs_from_attributes(attrs),
            extract_specs_from_title(base.name),
        )
        base.ram_gb = specs.ram_gb
        base.storage_gb = specs.storage_gb
        base.screen_inches = specs.screen_inches
        base.cpu = specs.cpu
        base.color = specs.color
        base.battery_mah = specs.battery_mah
        base.specs_confirmed = specs.confirmed

    seller = _extract_seller(ws)
    if seller:
        base.seller = seller
        base.seller_name = seller.seller_name
        base.seller_id = seller.seller_id
        base.seller_url = f"https://www.ozon.ru/seller/{seller.seller_id}/"
    return base


def _find_product_grid(widget_states: dict[str, Any]) -> dict | None:
    preferred = (
        "tileGridDesktop",
        "tileGrid",
        "searchResultsV2",
        "searchResults",
        "megaPaginator",
    )
    for key, raw in widget_states.items():
        if any(key.startswith(p) for p in preferred):
            obj = _as_dict(raw)
            if obj and (
                "items" in obj
                or "products" in obj
                or "mainState" in json.dumps(obj)[:500]
            ):
                return obj
    # fallback: любой виджет с плитками
    for key, raw in widget_states.items():
        obj = _as_dict(raw)
        if not obj:
            continue
        tiles = list(_walk_tile_items(obj))
        if tiles:
            return obj
    return None


def _walk_tile_items(node: Any):
    if isinstance(node, dict):
        if "mainState" in node and ("sku" in node or "id" in node):
            yield node
        items = node.get("items")
        if isinstance(items, list):
            for el in items:
                yield from _walk_tile_items(el)
        else:
            for v in node.values():
                yield from _walk_tile_items(v)
    elif isinstance(node, list):
        for el in node:
            yield from _walk_tile_items(el)


def _tile_to_search_item(tile: dict) -> SearchItem | None:
    pid = str(tile.get("sku") or tile.get("id") or "")
    if not pid.isdigit():
        return None

    link = (tile.get("action") or {}).get("link") or f"/product/{pid}/"
    link = link.split("?")[0]
    url = link if link.startswith("http") else f"https://www.ozon.ru{link}"

    name = None
    price = None
    old_price = None
    rating = None
    reviews = None

    for block in tile.get("mainState") or []:
        btype = block.get("type")
        if btype == "priceV2":
            for p in (block.get("priceV2") or {}).get("price") or []:
                text = p.get("text") or ""
                num = _digits(text)
                style = p.get("textStyle") or ""
                if style == "PRICE" and num and price is None:
                    price = num
                elif style in ("ORIGINAL_PRICE", "PRICE_STRIKETHROUGH") and num:
                    old_price = num
            if price is None:
                texts = [
                    p.get("text")
                    for p in (block.get("priceV2") or {}).get("price") or []
                    if p.get("text")
                ]
                if texts:
                    price = _digits(texts[0])
        elif btype == "textDS" and block.get("id") == "name":
            name = (block.get("textDS") or {}).get("text")
        elif btype == "labelListV2":
            auto = (
                ((block.get("labelListV2") or {}).get("testInfo") or {}).get(
                    "automatizationId"
                )
                or ""
            )
            texts = [
                (li.get("text") or {}).get("text") or ""
                for li in (block.get("labelListV2") or {}).get("items") or []
                if li.get("type") == "text"
            ]
            if auto == "tile-list-rating" and texts:
                try:
                    rating = float(texts[0].replace(",", "."))
                except ValueError:
                    pass
                if len(texts) > 1:
                    m = re.search(r"(\d[\d\s\u00a0\u202f]*)", texts[1])
                    if m:
                        reviews = int(re.sub(r"\D", "", m.group(1)) or "0")

    if not name or not price:
        return None

    delivery_title = _delivery_title(tile)
    has_fbo = bool(
        (tile.get("multiButton") or {}).get("ozonButton")
    ) and delivery_title in {"Завтра", "Сегодня"}

    return SearchItem(
        product_id=pid,
        name=name,
        price=price,
        old_price=old_price,
        rating=rating,
        reviews_count=reviews,
        url=url,
        seller_name="Ozon" if has_fbo else None,
        seller_id="ozon" if has_fbo else None,
        has_fbo=has_fbo,
        delivery_text=delivery_title,
    )


def _delivery_title(tile: dict) -> str | None:
    mb = tile.get("multiButton") or {}
    for key in ("ozonButton", "expressButton"):
        node = mb.get(key) or {}
        for val in node.values():
            if not isinstance(val, dict):
                continue
            ab = val.get("actionButton") or {}
            title = ab.get("title") or val.get("title")
            if title:
                return str(title)
    return None


def _parse_delivery(item: SearchItem) -> tuple[int | None, str]:
    # delivery days эвристика позже дополняется из плитки через имя;
    # здесь базово: FBO → 1 день
    if item.has_fbo:
        return 1, "FBO"
    return 3, "FBS"


def enrich_delivery_from_tile(product: ProductData, tile_title: str | None) -> None:
    if not tile_title:
        return
    t = tile_title.lower()
    if "сегодня" in t:
        product.delivery_days = 0
    elif "завтра" in t:
        product.delivery_days = 1
    else:
        # «2 августа» / «3 дн.»
        m = re.search(r"(\d+)\s*дн", t)
        if m:
            product.delivery_days = int(m.group(1))
        else:
            m = re.search(r"(\d{1,2})\s+(\w+)", tile_title)
            if m:
                product.delivery_days = _days_until_ru_date(m.group(1), m.group(2))


def _days_until_ru_date(day_s: str, month_s: str) -> int | None:
    months = {
        "января": 1,
        "февраля": 2,
        "марта": 3,
        "апреля": 4,
        "мая": 5,
        "июня": 6,
        "июля": 7,
        "августа": 8,
        "сентября": 9,
        "октября": 10,
        "ноября": 11,
        "декабря": 12,
    }
    month = months.get(month_s.lower())
    if not month:
        return None
    today = date.today()
    try:
        target = date(today.year, month, int(day_s))
    except ValueError:
        return None
    if target < today:
        target = date(today.year + 1, month, int(day_s))
    return (target - today).days


def _extract_characteristics(ws: dict[str, Any]) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for key, raw in ws.items():
        if not any(
            x in key.lower()
            for x in ("characteristic", "aspect", "shortinfo", "description")
        ):
            # всё равно просканируем на пары name/value
            pass
        text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        for m in re.finditer(
            r'"(?:name|title|key)"\s*:\s*"([^"]+)"\s*,\s*"(?:value|text)"\s*:\s*"([^"]+)"',
            text,
        ):
            attrs[m.group(1)] = m.group(2)
    return attrs


def _extract_seller(ws: dict[str, Any]) -> SellerData | None:
    for key, raw in ws.items():
        if "seller" not in key.lower() and "webSeller" not in key:
            continue
        obj = _as_dict(raw)
        if not obj:
            continue
        blob = json.dumps(obj, ensure_ascii=False)
        name_m = re.search(r'"name"\s*:\s*"([^"]{2,80})"', blob)
        id_m = re.search(r'"id"\s*:\s*"?(\d+)"?', blob)
        rating_m = re.search(r'"rating"\s*:\s*([0-9.]+)', blob)
        if name_m or id_m:
            return SellerData(
                seller_id=id_m.group(1) if id_m else "unknown",
                seller_name=name_m.group(1) if name_m else "Продавец",
                rating=float(rating_m.group(1)) if rating_m else None,
                is_verified="ozon" in (name_m.group(1).lower() if name_m else ""),
            )
    return None


def _as_dict(raw: Any) -> dict | None:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            obj = json.loads(raw)
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _digits(text: str) -> float | None:
    digits = re.sub(r"[^\d]", "", text or "")
    if not digits:
        return None
    return float(digits)
