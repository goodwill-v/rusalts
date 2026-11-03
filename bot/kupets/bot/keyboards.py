"""Inline-клавиатуры."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from models.result import SearchResponse


def build_cancel_report_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✖️ Отмена",
                    callback_data="cancel_report",
                )
            ]
        ]
    )


def build_result_keyboard(response: SearchResponse) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    rows.append(
        [InlineKeyboardButton(text="🔄 Новый поиск", callback_data="new_search")]
    )

    # Уточнения
    refine: list[InlineKeyboardButton] = [
        InlineKeyboardButton(text="📦 Только FBO", callback_data="refine:fbo"),
    ]
    if response.filters.price_max:
        tighter = int(response.filters.price_max * 0.75)
        if tighter >= 1000:
            refine.append(
                InlineKeyboardButton(
                    text=f"💰 До {tighter:,} ₽".replace(",", " "),
                    callback_data=f"refine:price:{tighter}",
                )
            )
    brands = _top_brands(response)
    if brands:
        refine.append(
            InlineKeyboardButton(
                text=f"🏷 {brands[0]}",
                callback_data=f"refine:brand:{brands[0]}",
            )
        )
    rows.append(refine[:3])

    detail_row: list[InlineKeyboardButton] = []
    for i, sp in enumerate(response.scored[:3]):
        detail_row.append(
            InlineKeyboardButton(
                text=f"📊 №{i + 1}",
                callback_data=f"detail:{i}",
            )
        )
    if detail_row:
        rows.append(detail_row)

    mid: list[InlineKeyboardButton] = []
    if len(response.scored) > 3:
        mid.append(
            InlineKeyboardButton(
                text=f"📋 Все {len(response.scored)}",
                callback_data="show_all:0",
            )
        )
    if response.rejected:
        mid.append(
            InlineKeyboardButton(
                text="❌ Почему отсеяли",
                callback_data="show_rejects",
            )
        )
    if mid:
        rows.append(mid)

    link_row: list[InlineKeyboardButton] = []
    for i, sp in enumerate(response.scored[:3]):
        link_row.append(
            InlineKeyboardButton(
                text=f"🔗 Товар {i + 1}",
                url=sp.product.url,
            )
        )
    if link_row:
        rows.append(link_row)

    rows.append(
        [
            InlineKeyboardButton(
                text="⚠️ Сообщить об ошибке или предложить улучшение",
                callback_data="report_error",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_paging_keyboard(page: int, total: int, page_size: int = 5) -> InlineKeyboardMarkup:
    buttons: list[InlineKeyboardButton] = []
    if page > 0:
        buttons.append(
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=f"show_all:{page - 1}",
            )
        )
    if (page + 1) * page_size < total:
        buttons.append(
            InlineKeyboardButton(
                text="Вперёд ➡️",
                callback_data=f"show_all:{page + 1}",
            )
        )
    rows = [buttons] if buttons else []
    rows.append(
        [InlineKeyboardButton(text="🔄 Новый поиск", callback_data="new_search")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _top_brands(response: SearchResponse) -> list[str]:
    known = (
        "Xiaomi",
        "Samsung",
        "Redmi",
        "POCO",
        "Realme",
        "Tecno",
        "Infinix",
        "Honor",
        "Huawei",
        "Apple",
        "vivo",
        "OPPO",
        "Nokia",
        "Motorola",
    )
    counts: dict[str, int] = {}
    for sp in response.scored + response.near_miss:
        name = sp.product.name
        for b in known:
            if b.lower() in name.lower():
                counts[b] = counts.get(b, 0) + 1
                break
    return [b for b, _ in sorted(counts.items(), key=lambda x: -x[1])]
