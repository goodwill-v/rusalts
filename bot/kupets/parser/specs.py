"""Извлечение характеристик из спецификаций и названия."""

from __future__ import annotations

import re

from models.product import ProductSpecs


def extract_specs_from_attributes(attrs: dict[str, str]) -> ProductSpecs:
    """attrs: нормализованные ключи → значения из карточки."""
    ram = _find_number(
        attrs,
        keys=("оперативн", "озу", "ram"),
        unit_gb=True,
    )
    storage = _find_number(
        attrs,
        keys=("встроенн", "память", "накопител", "rom", "ssd", "пзу"),
        unit_gb=True,
    )
    screen = _find_number(
        attrs,
        keys=("диагонал", "экран", "дисплей", "screen"),
        unit_inches=True,
    )
    cpu = _find_str(attrs, ("процессор", "cpu", "чипсет"))
    color = _find_str(attrs, ("цвет", "color"))
    battery = _find_int(attrs, ("батаре", "аккумулятор", "ёмкост", "емкост"))

    confirmed = any(v is not None for v in (ram, storage, screen))
    return ProductSpecs(
        ram_gb=ram,
        storage_gb=storage,
        screen_inches=screen,
        cpu=cpu,
        color=color,
        battery_mah=battery,
        confirmed=confirmed,
    )


def extract_specs_from_title(title: str) -> ProductSpecs:
    """Fallback: «8/128 ГБ», «экран 6.6"» из названия."""
    ram = storage = screen = None
    m = re.search(
        r"(\d+)\s*/\s*(\d+)\s*(?:г[бb]|gb)?",
        title,
        re.IGNORECASE,
    )
    if m:
        ram = float(m.group(1))
        storage = float(m.group(2))

    m = re.search(
        r"(\d+(?:[.,]\d+)?)\s*(?:г[бb]|gb)\s*(?:оператив|озу|ram|/)",
        title,
        re.IGNORECASE,
    )
    if m and ram is None:
        ram = float(m.group(1).replace(",", "."))

    m = re.search(
        r"(?:экран|дисплей|диагональ)?\s*(\d+(?:[.,]\d+)?)\s*(?:\"|''|дюйм)",
        title,
        re.IGNORECASE,
    )
    if m:
        screen = float(m.group(1).replace(",", "."))

    confirmed = any(v is not None for v in (ram, storage, screen))
    return ProductSpecs(
        ram_gb=ram,
        storage_gb=storage,
        screen_inches=screen,
        confirmed=confirmed,
    )


def merge_specs(primary: ProductSpecs, fallback: ProductSpecs) -> ProductSpecs:
    """Приоритет: спецификации → название."""
    return ProductSpecs(
        ram_gb=primary.ram_gb if primary.ram_gb is not None else fallback.ram_gb,
        storage_gb=(
            primary.storage_gb
            if primary.storage_gb is not None
            else fallback.storage_gb
        ),
        screen_inches=(
            primary.screen_inches
            if primary.screen_inches is not None
            else fallback.screen_inches
        ),
        cpu=primary.cpu or fallback.cpu,
        color=primary.color or fallback.color,
        battery_mah=primary.battery_mah or fallback.battery_mah,
        confirmed=primary.confirmed or fallback.confirmed,
    )


def _find_number(
    attrs: dict[str, str],
    keys: tuple[str, ...],
    *,
    unit_gb: bool = False,
    unit_inches: bool = False,
) -> float | None:
    for key, val in attrs.items():
        lk = key.lower()
        if not any(k in lk for k in keys):
            continue
        # пропускаем «встроенная память», если ищем ОЗУ, и наоборот — по ключам
        m = re.search(r"(\d+(?:[.,]\d+)?)", val.replace(" ", ""))
        if not m:
            continue
        num = float(m.group(1).replace(",", "."))
        if unit_gb and re.search(r"т[бb]|tb", val, re.IGNORECASE):
            num *= 1024
        if unit_inches and num > 20:
            continue
        return num
    return None


def _find_str(attrs: dict[str, str], keys: tuple[str, ...]) -> str | None:
    for key, val in attrs.items():
        if any(k in key.lower() for k in keys) and val.strip():
            return val.strip()
    return None


def _find_int(attrs: dict[str, str], keys: tuple[str, ...]) -> int | None:
    for key, val in attrs.items():
        if not any(k in key.lower() for k in keys):
            continue
        m = re.search(r"(\d+)", val.replace(" ", ""))
        if m:
            return int(m.group(1))
    return None
