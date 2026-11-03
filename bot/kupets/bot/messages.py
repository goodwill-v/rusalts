"""Шаблоны сообщений Markdown."""

from __future__ import annotations

from collections import Counter

from models.result import ScoredProduct, SearchResponse
from utils import format_price

MEDALS = ("🥇", "🥈", "🥉")


def start_message() -> str:
    return (
        "🛒 *Купец*\n"
        "Помогу найти лучший товар на Ozon.\n\n"
        "Просто напиши, что ищешь:\n"
        "«найди андроид с оперативкой от 8ГБ, до 20000 руб»\n\n"
        "⭐ *SCORE* — рейтинг по 4 показателям, "
        "включая цену и надёжность продавца.\n\n"
        "Или отправь *фото* — распознаю товар и найду похожие на Ozon."
    )


def format_results(response: SearchResponse) -> str:
    if response.ozon_unavailable:
        q = _md(response.query_display or response.filters.text or "запрос")
        wait = response.ozon_retry_sec
        wait_line = (
            f"Попробуйте через ~{wait} сек или чуть позже."
            if wait > 0
            else "Попробуйте чуть позже."
        )
        return (
            "⚠️ *Ozon временно недоступен для бота*\n\n"
            "Антибот-защита заблокировала запрос — это *не значит*, "
            "что товара нет на Ozon.\n"
            f"Запрос: _{q}_\n\n"
            f"{wait_line}"
        )

    if response.found_total == 0:
        return (
            "😕 Ничего не нашёл по этому запросу.\n"
            "Попробуй переформулировать или смягчить критерии."
        )

    if not response.scored and not response.near_miss:
        reasons = _summarize_rejects(response)
        return (
            f"🔍 *{_md(response.query_display)}* — найдено "
            f"{response.found_total} предложений\n\n"
            f"❌ Всё отсеялось ({len(response.rejected)}): {reasons}\n\n"
            "Попробуй смягчить запрос или нажми «Почему отсеяли»."
        )

    lines: list[str] = []
    q = _md(response.query_display)
    lines.append(
        f"🔍 *{q}* — найдено {response.found_total} предложений\n"
    )
    lines.append(
        f"✅ Прошли отбор: {len(response.scored)} из {response.found_total}"
    )
    if response.rejected:
        lines.append(
            f"❌ Отсеяно: {len(response.rejected)} "
            f"({_summarize_rejects(response)})"
        )
    if response.near_miss:
        lines.append(
            f"🔸 Почти подошло: {len(response.near_miss)}"
        )

    lines.append(
        "\n⭐ _SCORE — рейтинг по 4 показателям, "
        "включая цену и надёжность продавца._"
    )
    lines.append("\n────────────────")

    top = response.scored[:3]
    for i, sp in enumerate(top):
        lines.append("")
        lines.append(_format_product_card(sp, i))

    if response.near_miss:
        lines.append("\n────────────────")
        lines.append("🔸 *Почти подошло*")
        for sp in response.near_miss[:3]:
            reason = (
                "; ".join(sp.product.near_miss_reasons)
                or "; ".join(sp.match_notes)
                or "близко к критериям"
            )
            lines.append(f"• *{_md(sp.product.name)}* — ⚠️ {_md(reason)}")

    lines.append("\n────────────────")
    lines.append("*Хочешь уточнить выдачу кнопками ниже?*")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3990] + "…"
    return text


def format_reject_details(response: SearchResponse, limit: int = 12) -> str:
    if not response.rejected:
        return "Отсечённых товаров нет."
    lines = [f"❌ *Почему отсеяли* ({len(response.rejected)}):\n"]
    for p, reasons in response.rejected[:limit]:
        reason = "; ".join(reasons) if reasons else "фильтр"
        lines.append(f"• {_md(p.name[:70])} — _{_md(reason)}_")
    if len(response.rejected) > limit:
        lines.append(f"\n…и ещё {len(response.rejected) - limit}")
    return "\n".join(lines)


def format_product_detail(sp: ScoredProduct, index: int = 0) -> str:
    p = sp.product
    b = sp.breakdown
    notes = "; ".join(sp.match_notes) or "—"
    return (
        f"📊 *{_md(p.name)}* — SCORE {sp.score}/100\n\n"
        f"├ Продавец:  {b.seller:.0f}/35\n"
        f"├ Цена:      {b.price:.0f}/30\n"
        f"├ Отзывы:    {b.reviews:.0f}/25\n"
        f"└ Доставка:  {b.delivery:.0f}/10"
        + (f"\n   Штраф: −{b.soft_penalty:.0f}" if b.soft_penalty else "")
        + f"\n\n{_md(notes)}\n"
        f"🔗 [Открыть на Ozon]({p.url})"
    )


def format_all_results(response: SearchResponse, page: int = 0, page_size: int = 5) -> str:
    items = response.scored
    if not items:
        return "Нет прошедших отбор товаров."
    start = page * page_size
    chunk = items[start : start + page_size]
    if not chunk:
        return "Больше товаров нет."

    lines = [f"📋 Все прошедшие отбор (стр. {page + 1}):\n"]
    for i, sp in enumerate(chunk, start=start + 1):
        p = sp.product
        lines.append(
            f"{i}. *{_md(p.name)}* · SCORE *{sp.score}*\n"
            f"   {format_price(p.price)} · [ссылка]({p.url})"
        )
    return "\n".join(lines)


def _format_product_card(sp: ScoredProduct, index: int) -> str:
    p = sp.product
    medal = MEDALS[index] if index < len(MEDALS) else f"#{index + 1}"
    badge = "🔥" if sp.score >= 85 else ("✅" if sp.score >= 65 else "⚠️")
    b = sp.breakdown

    seller_rating = ""
    if p.seller and p.seller.rating is not None:
        seller_rating = f" · {p.seller.rating} ☆"
    orders = ""
    if p.seller and p.seller.orders_count:
        orders = f" · {p.seller.orders_count:,}+ заказов".replace(",", "\u202f")

    delivery = _delivery_line(p.delivery_days, p.delivery_price)
    fbo = f"📦 {p.delivery_type}" + (
        " (склад Ozon)" if p.delivery_type == "FBO" else ""
    )

    rating_line = "—"
    if p.rating is not None:
        rating_line = f"{p.rating} ({p.reviews_count:,} отзывов)".replace(
            ",", "\u202f"
        )

    verdict = sp.verdict or ("; ".join(sp.match_notes) if sp.match_notes else "")
    if verdict:
        mark = "✅" if sp.score >= 65 and not p.near_miss_reasons else "⚠️"
        verdict_line = f"{mark} {_md(verdict)}"
    else:
        verdict_line = ""

    score_line = (
        f"📊 SCORE: продавец {b.seller:.0f} · цена {b.price:.0f} · "
        f"отзывы {b.reviews:.0f} · доставка {b.delivery:.0f}"
    )

    parts = [
        f"{medal} *{_md(p.name)}* · ⭐ SCORE *{sp.score} / 100* {badge}",
        score_line,
        f"💰 {format_price(p.price)} | Рейтинг товара {rating_line}",
        f"🏪 Продавец: {_md(p.seller_name or '—')}{seller_rating}{orders}",
        f"🚚 Доставка: {delivery}",
        fbo,
        f"[ozon.ru/product/{p.product_id}]({p.url})",
    ]
    if verdict_line:
        parts.append(verdict_line)
    return "\n".join(parts)


def _delivery_line(days: int | None, price: float) -> str:
    if days is None:
        when = "срок уточняется"
    elif days <= 1:
        when = "завтра"
    else:
        when = f"{days} дн."
    cost = "бесплатно" if price == 0 else f"{int(price)} ₽"
    return f"{when} · {cost}"


def _summarize_rejects(response: SearchResponse) -> str:
    counter: Counter[str] = Counter()
    for _, reasons in response.rejected:
        for r in reasons:
            key = r
            if "оператив" in r:
                key = "оперативка"
            elif "память" in r.lower():
                key = "память"
            elif "экран" in r:
                key = "экран"
            elif "нерелевант" in r or "клон" in r or "аксессуар" in r or "мусор" in r:
                key = "мусор / клон"
            elif "FBO" in r:
                key = "не FBO"
            elif "низкая цена" in r or "скидка" in r:
                key = "подозрительно низкая цена"
            elif "завышена" in r:
                key = "завышенная цена"
            elif "рейтинг" in r:
                key = "низкий рейтинг"
            elif "цена выше" in r or "цена ниже" in r:
                key = "цена вне диапазона"
            counter[key] += 1
    if not counter:
        return "по фильтрам"
    parts = [f"{n} — {k}" for k, n in counter.most_common(5)]
    return ", ".join(parts)


def _md(text: str) -> str:
    """Минимальное экранирование для Telegram Markdown (legacy)."""
    return (
        text.replace("*", "∗")
        .replace("_", "＿")
        .replace("`", "'")
        .replace("[", "(")
        .replace("]", ")")
    )
