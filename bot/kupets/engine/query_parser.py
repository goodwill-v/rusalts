"""Извлечение фильтров из естественного языкового запроса."""

from __future__ import annotations

import re

from models.result import SearchFilters

KNOWN_BRANDS = (
    "samsung",
    "xiaomi",
    "redmi",
    "poco",
    "apple",
    "iphone",
    "huawei",
    "honor",
    "realme",
    "oppo",
    "vivo",
    "nokia",
    "motorola",
    "sony",
    "google",
    "pixel",
    "tecno",
    "infinix",
    "oneplus",
    "asus",
    "lenovo",
)


def _parse_price(raw: str) -> float:
    cleaned = raw.replace(" ", "").replace("\u202f", "").replace(",", ".")
    # «20000» или «20.000» / «20,000»
    if cleaned.count(".") == 1 and len(cleaned.split(".")[-1]) == 3:
        cleaned = cleaned.replace(".", "")
    return float(cleaned)


def _extract_brand(query: str) -> str | None:
    lower = query.lower()
    for brand in KNOWN_BRANDS:
        if re.search(rf"\b{re.escape(brand)}\b", lower):
            return brand.capitalize() if brand != "iphone" else "Apple"
    return None


def extract_filters(query: str) -> SearchFilters:
    """Извлекает фильтры из естественного запроса.

    Пример:
    «найди андроид с оперативкой от 8ГБ, памятью от 128 ГБ
     и экраном от 5,5 дюймов до 20000 руб»
    → price_max=20000, ram_gb=8, storage_gb=128, screen_inches=5.5
    """
    text = query.lower().strip()
    price_min: float | None = None
    price_max: float | None = None

    m = re.search(
        r"до\s+(\d+(?:[\s.,]?\d+)*)\s*(?:руб|₽|р\.?)?",
        text,
        re.IGNORECASE,
    )
    if m:
        price_max = _parse_price(m.group(1))

    m = re.search(
        r"от\s+(\d+(?:[\s.,]?\d+)*)\s*(?:до\s+(\d+(?:[\s.,]?\d+)*))?\s*"
        r"(?:руб|₽|р\.?)?",
        text,
        re.IGNORECASE,
    )
    if m:
        # «от 8ГБ» не цена — проверяем контекст рядом
        start = m.start()
        snippet = text[max(0, start - 20) : m.end() + 5]
        if not re.search(r"(оператив|озу|ram|памят|г[бb]|дюйм|экран)", snippet):
            price_min = _parse_price(m.group(1))
            if m.group(2):
                price_max = _parse_price(m.group(2))

    m = re.search(
        r"(\d+(?:[\s.,]?\d+)*)\s*[-–—]\s*(\d+(?:[\s.,]?\d+)*)\s*(?:руб|₽)",
        text,
        re.IGNORECASE,
    )
    if m:
        price_min = _parse_price(m.group(1))
        price_max = _parse_price(m.group(2))

    ram_gb: float | None = None
    m = re.search(
        r"(?:оперативк\w*|ram|озу)"
        r"[^\d]{0,20}(?:от\s+)?(\d+(?:[.,]\d+)?)\s*(?:г[бb]|gb)",
        text,
        re.IGNORECASE,
    )
    if m:
        ram_gb = float(m.group(1).replace(",", "."))
    else:
        m = re.search(
            r"(\d+(?:[.,]\d+)?)\s*(?:г[бb]|gb)\s*(?:оператив|озу|ram)",
            text,
            re.IGNORECASE,
        )
        if m:
            ram_gb = float(m.group(1).replace(",", "."))

    storage_gb: float | None = None
    m = re.search(
        r"(?:встроенн\w*\s*памят\w*|память|storage|rom|накопител\w*|пзу|ssd)"
        r"[^\d]{0,20}(?:от\s+)?(\d+(?:[.,]\d+)?)\s*(?:г[бb]|gb|т[бb]|tb)",
        text,
        re.IGNORECASE,
    )
    if m:
        storage_gb = float(m.group(1).replace(",", "."))
        if re.search(r"т[бb]|tb", m.group(0), re.IGNORECASE):
            storage_gb *= 1024

    # Паттерн «8/128» в запросе
    m = re.search(r"(\d+)\s*/\s*(\d+)\s*(?:г[бb]|gb)?", text, re.IGNORECASE)
    if m:
        if ram_gb is None:
            ram_gb = float(m.group(1))
        if storage_gb is None:
            storage_gb = float(m.group(2))

    screen_inches: float | None = None
    m = re.search(
        r"(?:экран|диагонал\w*|дисплей|screen)"
        r"[^\d]{0,20}(?:от\s+)?(\d+(?:[.,]\d+)?)\s*(?:дюйм\w*|inch|\"|'')?",
        text,
        re.IGNORECASE,
    )
    if m:
        screen_inches = float(m.group(1).replace(",", "."))

    # Очищаем текст для поиска Ozon: сначала характеристики, потом цены
    search_text = text
    search_text = re.sub(
        r"(?:найди|найти|ищу|хочу|подбери|подберите)\s*",
        "",
        search_text,
        flags=re.IGNORECASE,
    )
    search_text = re.sub(
        r"(?:оперативк\w*|ram|озу|встроенн\w*\s*памят\w*|память|storage|rom|"
        r"накопител\w*|экран|диагонал\w*|дисплей|screen)"
        r"[^\d]{0,24}(?:от\s+)?\d+(?:[.,]\d+)?\s*"
        r"(?:г[бb]|gb|т[бb]|tb|дюйм\w*|inch|\"|'')?",
        " ",
        search_text,
        flags=re.IGNORECASE,
    )
    search_text = re.sub(
        r"\d+(?:[.,]\d+)?\s*(?:г[бb]|gb|т[бb]|tb|дюйм\w*|inch)",
        " ",
        search_text,
        flags=re.IGNORECASE,
    )
    search_text = re.sub(r"\d+\s*/\s*\d+", " ", search_text)
    # цена только с явным маркером валюты или «до N»
    search_text = re.sub(
        r"(?:до|от)\s+[\d\s.,]+\s*(?:руб|₽|р\.?)",
        " ",
        search_text,
        flags=re.IGNORECASE,
    )
    search_text = re.sub(
        r"до\s+[\d\s.,]+",
        " ",
        search_text,
        flags=re.IGNORECASE,
    )
    search_text = re.sub(
        r"\b(?:с|и|на|для|до|от|по|или)\b",
        " ",
        search_text,
        flags=re.IGNORECASE,
    )
    search_text = re.sub(
        r"\b(?:оперативк\w*|озу|ram|память|встроенн\w*|экран|дисплей|"
        r"диагонал\w*|г[бb]|gb|дюйм\w*)\b",
        " ",
        search_text,
        flags=re.IGNORECASE,
    )
    search_text = re.sub(r"[\s,;:]+", " ", search_text).strip()

    brand = _extract_brand(query)
    if not search_text:
        search_text = brand.lower() if brand else query.strip()

    return SearchFilters(
        text=search_text,
        price_max=price_max,
        price_min=price_min,
        ram_gb=ram_gb,
        storage_gb=storage_gb,
        screen_inches=screen_inches,
        brand=brand,
    )
