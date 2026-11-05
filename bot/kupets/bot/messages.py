"""Шаблоны сообщений Markdown."""

from __future__ import annotations

from collections import Counter

from models.result import ScoredProduct, SearchResponse
from utils import format_price

MEDALS = ("🥇", "🥈", "🥉")


def start_message() -> str:
    return (
        "🛒 *Купец*\n"
        "Найду и сравню товары на Ozon, Wildberries и Яндекс.Маркете.\n\n"
        "Просто напиши, что ищешь:\n"
        "«найди андроид с оперативкой от 8ГБ, до 20000 руб»\n"
        "«термос для школы до 2000»\n\n"
        "⭐ *SCORE* — рейтинг по 4 показателям, "
        "включая цену и надёжность продавца. Если цены в выдаче нет — "
        "она по ссылке на товар.\n\n"
        "Или отправь *фото* — распознаю товар и найду похожий.\n\n"
        "🔔 *Жду скидку*: /watch <запрос> — раз в сутки проверю "
        "минимальную цену и сообщу, когда она изменится. "
        "Список слежек: /watches, отключить: /unwatch <номер>."
    )


def quota_exhausted_message(info: dict) -> str:
    """Вежливый отказ при исчерпании квоты поисков (дневной/месячной, с тарифом)."""
    plan = info.get("plan") or "free"
    used = info.get("used", 0)
    limit = info.get("limit", 0)
    period = info.get("period") or "day"
    plan_name = info.get("plan_name") or ("Покупатель" if plan == "kupec" else "")
    upsell = ""
    if plan != "free":
        tariff_line = f"тарифе «{plan_name or 'Покупатель'}»"
    else:
        tariff_line = "бесплатном тарифе"
        upsell = (
            "\n\nНа платных тарифах — поиски на месяц, слежка за ценой и "
            "сводки. Подробнее: rusalts.ru"
        )
    if period == "month":
        return (
            f"⏳ *Месячный лимит поисков исчерпан* — {used} из {limit} "
            f"на {tariff_line}.\n\nКвота обнуляется 1-го числа.{upsell}"
        )
    return (
        f"⏳ *Дневной лимит поисков исчерпан* — {used} из {limit} "
        f"на {tariff_line}.\n\nКвота обнуляется в 00:00 по Москве.{upsell}"
    )




def feature_limit_message(feature_label: str, info: dict) -> str:
    """Вежливый отказ по лимиту фичи (месяц/день) + апселл."""
    period = "в этом месяце" if info.get("period") == "month" else "сегодня"
    plan = info.get("plan_name") or "Покупатель"
    return (
        f"⚠️ Лимит «{_md(feature_label)}» на тарифе «{_md(plan)}» "
        f"исчерпан: {info.get('used', 0)} из {info.get('limit', 0)} {period}.\n\n"
        "Посмотреть тарифы: rusalts.ru"
    )


def compare_result_message(
    qa: str,
    qb: str,
    a: dict | None,
    b: dict | None,
    ai_verdict: str,
    searches_note: str,
) -> str:
    """Таблица сравнения X vs Y (ТЗ v2 фича б). a/b: {name, price, rating,
    reviews_count, marketplace, url} или None, если сторона пустая."""

    def side(letter, q, d):
        if d is None:
            return f"{letter} *{_md(q)}*\n   — не нашёл, переформулируйте"
        mp = {"ozon": "Ozon", "wb": "Wildberries", "yam": "Я.Маркет"}.get(
            d.get("marketplace", "ozon"), "Ozon"
        )
        price = (
            format_price(d["price"]) if d.get("price") else "цена по ссылке"
        )
        rating = (
            f"{d['rating']} ★ ({d.get('reviews_count') or 0} отз.)"
            if d.get("rating")
            else "рейтинг не виден"
        )
        link = f"[{_md(d['name'][:40])}]({d['url']})" if d.get("url") else _md(d["name"][:50])
        return (
            f"{letter} {link}\n"
            f"   {price} · {rating} · {mp}"
        )

    lines = [
        "⚖️ *Сравнение*",
        "",
        side("🅰", qa, a),
        "",
        side("🅱", qb, b),
        "",
        f"📌 *Итог:* {_md(ai_verdict)}",
        "",
        f"_💡 {searches_note}_",
    ]
    return "\n".join(lines)


def check_result_message(name: str, verdict: dict) -> str:
    """Вердикт анти-клон проверки (ТЗ v2 фича в)."""
    level_icon = {"ok": "✅", "warn": "🟡", "danger": "🔴"}.get(
        verdict.get("level", "ok"), "✅"
    )
    level_text = {
        "ok": "явных признаков подделки не нашёл",
        "warn": "есть подозрительные признаки",
        "danger": "высокая вероятность подделки/клона",
    }.get(verdict.get("level", "ok"), "")
    lines = [
        f"🛡 *Проверка: {_md(name[:60])}*",
        "",
        f"{level_icon} Вердикт: *{_md(level_text)}*",
        "",
    ]
    for r in verdict.get("reasons", []):
        lines.append(f"• {_md(r)}")
    lines.append("")
    lines.append(f"Уверенность: {_md(verdict.get('confidence', 'средняя'))}. "
                 "Проверка по сниппетам поиска — не гарантия.")
    return "\n".join(lines)


def gift_message(description: str, budget: int, picks: list[tuple[str, object]]) -> str:
    """Подборка подарков (ТЗ v2 фича г). picks: [(запрос, SearchItem|None)]."""
    mp_labels = {"ozon": "Ozon", "wb": "Wildberries", "yam": "Я.Маркет"}
    lines = [
        f"🎁 *Подарки: {_md(description)}* — до {format_price(budget)}",
        "",
    ]
    found = 0
    for i, (q, item) in enumerate(picks, 1):
        if item is None:
            lines.append(f"{i}. {q} — не нашёл")
            continue
        found += 1
        mp = mp_labels.get(getattr(item, "marketplace", "ozon"), "Ozon")
        price = format_price(item.price) if item.price > 0 else "цена по ссылке"
        lines.append(
            f"{i}. *{_md(item.name[:44])}* — {price} · {mp}\n"
            f"   [открыть]({item.url})"
        )
    if not found:
        return (
            "😔 Ничего не нашёл по подбору. Попробуйте изменить бюджет "
            "или описание: /gift маме до 2000"
        )
    lines.append("")
    lines.append("_💡 Подбор потратил до 4 поисков из вашего месячного пакета._")
    return "\n".join(lines)


def gift_usage_message() -> str:
    return (
        "Опишите, кому подарок и бюджет:\n"
        "/gift маме до 2000\n/gift мальчику 7 лет до 3000"
    )


def report_message(stats: dict, ai_summary: str = "") -> str:
    """Сводка для продавцов v2 (ТЗ v2 §3.6): цифры, распределение, резюме.

    stats — services.report.build_report (многозапросная сборка).
    """
    mp_labels = {"ozon": "Ozon", "wb": "Wildberries", "yam": "Я.Маркет"}
    niche = stats["niche"]
    if stats["total"] == 0:
        return (
            f"📊 Ниша «{_md(niche)}»: по запросу ничего не нашлось — "
            "попробуйте другую формулировку."
        )
    n, priced_count = stats["total"], stats["priced_count"]
    lines = [
        f"📊 *Сводка для продавца: {_md(niche)}*",
        "",
        f"📦 Карточек собрано: {n} — "
        + " · ".join(
            f"{mp_labels.get(k, k)}: {v}" for k, v in stats["per_marketplace"].items()
        ),
    ]

    # Цены: коридор + медиана + разбивка по площадкам
    if stats["median"]:
        samples_note = (
            f" + {stats['sample_count']} из сниппетов каталогов"
            if stats.get("sample_count")
            else ""
        )
        lines.append(
            f"💰 Цены ({priced_count} карточек{samples_note}): "
            f"от {format_price(stats['min'])} · медиана "
            f"*{format_price(stats['median'])}* · до {format_price(stats['max'])}"
        )
        mp_med = [
            f"{mp_labels.get(mp, mp)} ~{format_price(d['median'])} ({d['count']})"
            for mp, d in (stats.get("mp_medians") or {}).items()
        ]
        if mp_med:
            lines.append("   По площадкам: " + " · ".join(mp_med))
        if stats.get("recommended_price"):
            lines.append(
                f"🎯 Рекомендуемая цена входа: ~{format_price(stats['recommended_price'])} "
                "(чуть ниже медианы)"
            )
        buckets = stats.get("price_buckets") or {}
        if buckets:
            lines.append("   Распределение: " + " · ".join(f"{k}: {v}" for k, v in buckets.items()))
    else:
        lines.append(f"💰 Цены в сниппетах не видны ({n} карточек — смотрите по ссылкам)")

    # Рейтинги и конкуренция
    if stats.get("rated_count"):
        lines.append(
            f"⭐ Рейтинги: у {stats['rated_count']} из {n}, средний {stats['avg_rating']}"
        )
    lines.append(f"📈 Конкуренция: {_md(stats.get('competition', '—'))}")
    if stats.get("rooted_count"):
        lines.append(f"   Укоренившиеся (>100 отзывов): {stats['rooted_count']}")
    lines.append(f"📉 Демпинг (<30% медианы): {stats['dump_count']}")
    lines.append(f"⚠️ Клоны/мусорные названия: {stats['clone_count']}")

    # ИИ-резюме (или вычисляемый итог)
    summary = ai_summary or (
        f"Рекомендуемая цена входа ~{format_price(stats['recommended_price'])}."
        if stats.get("recommended_price") else ""
    )
    if summary:
        lines += ["", f"🤖 *Резюме для продавца:* {_md(summary)}"]

    lines += ["", "*Топ карточек по рейтингу и отзывам:*"]
    for i, it in enumerate(stats["top"], 1):
        price = format_price(it.price) if it.price > 0 else "цена по ссылке"
        rating = f"{it.rating}★" if it.rating else "—"
        reviews = f"{it.reviews_count} отз." if it.reviews_count else "—"
        mp = mp_labels.get(getattr(it, "marketplace", "ozon"), "Ozon")
        lines.append(
            f"{i}. {_md(it.name[:40])} — {price} · {rating} · {reviews} · {mp}\n"
            f"   [ссылка]({it.url})"
        )
    lines += ["", "_💡 Лимит: 100 сводок/мес. Поиски пакета сводка не расходует._"]
    return "\n".join(lines)


def report_usage_message() -> str:
    return (
        "Укажите нишу: /report термос для школы\n"
        "Сводка доступна на тарифе «Маркетинг» (10 в месяц). rusalts.ru"
    )


def watch_usage_message() -> str:
    return (
        "Укажите, за чем следить: /watch <запрос>\n"
        "Например: /watch термос для школы до 2000"
    )


def watch_not_available_message() -> str:
    return (
        "🔒 *Слежка за ценой* («Жду скидку») доступна на тарифе «Покупатель».\n\n"
        "На бесплатном тарифе — 3 поиска в день, без слежки.\n"
        "«Покупатель» (150 ₽/мес, триал 7 дней): 6 поисков в день "
        "и слежка за 5 товарами.\n"
        "Оформить: rusalts.ru"
    )


def watch_limit_message(info: dict) -> str:
    plan = info.get("plan_name") or "Покупатель"
    return (
        f"⚠️ Лимит слежек тарифа «{_md(plan)}» исчерпан — "
        f"{info.get('count', 5)} из {info.get('limit', 5)}.\n\n"
        "Отключите одну: /unwatch <номер> (список: /watches), "
        "потом добавьте новую.\n"
        "Нужно больше — посмотрите тарифы: rusalts.ru"
    )


def watch_added_message(query: str, info: dict) -> str:
    return (
        "🔔 *Слежка включена*\n\n"
        f"Запрос: _{_md(query)}_\n\n"
        "Раз в сутки утром (07:00–09:00 МСК) проверю минимальную цену "
        "по Ozon, Wildberries и Яндекс.Маркету. Напишу только когда цена "
        "изменится — без лишнего шума.\n"
        f"Занято слежек: {info.get('count', 0) + 1} из {info.get('limit', 5)} "
        f"(тариф «{_md(info.get('plan_name') or 'Покупатель')}»).\n\n"
        "Список: /watches · Отключить: /unwatch <номер>"
    )


def watch_duplicate_message(query: str) -> str:
    return f"Вы уже следите за _{_md(query)}_. Список слежек: /watches"


def watches_list_message(watches: list[dict], info: dict) -> str:
    if not watches:
        return (
            "Слежек пока нет.\n\n"
            "Добавить: /watch <запрос> — например, "
            "/watch термос для школы до 2000"
        )
    from datetime import datetime

    from utils import format_price

    from services.quotas import MOSCOW

    lines = [
        f"🔔 *Ваши слежки* ({len(watches)} из {info.get('limit', 5)}):\n"
    ]
    for w in watches:
        price_part = "цена ещё не проверялась"
        if w.get("last_min_price") is not None:
            lp = w["last_min_price"]
            price_part = (
                "цены в выдаче не было" if lp <= 0 else format_price(lp)
            )
        checked_part = "не проверялась"
        if w.get("last_check_at"):
            checked_part = datetime.fromtimestamp(
                w["last_check_at"], tz=MOSCOW
            ).strftime("проверена %d.%m в %H:%M МСК")
        lines.append(
            f"{w['id']}. {_md(w['query'])} — 💰 {price_part} ({checked_part})"
        )
    lines.append("\nОтключить: /unwatch <номер>")
    return "\n".join(lines)


def watch_disabled_message(watch: dict) -> str:
    return (
        f"✅ Слежка №{watch['id']} отключена: _{_md(watch['query'])}_.\n"
        "Вернуть можно новой командой /watch."
    )


def watch_not_found_message(watch_id: int) -> str:
    return (
        f"Не нашёл активную слежку №{watch_id}.\n"
        "Посмотрите номера: /watches"
    )


def watch_hint_message() -> str:
    return (
        "Чтобы отключить слежку, напишите: /unwatch <номер>\n"
        "Номера — в списке /watches"
    )


def format_results(response: SearchResponse) -> str:
    if getattr(response, "provider_limited", False):
        return (
            "🛠 *Поиск временно приостановлен*\n\n"
            "У сервиса закончился месячный лимит запросов к поисковым системам.\n"
            "Мы уже знаем об этом и пополняем тариф — обычно это занимает пару часов.\n\n"
            "Напишите /start чуть позже или попробуйте снова."
        )
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

    spread = _price_spread_block(response)
    if spread:
        lines.append(spread)
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


MP_LABELS = {"ozon": "Ozon", "wb": "Wildberries", "yam": "Я.Маркет"}


def _price_spread_block(response: SearchResponse) -> str:
    """«Разброс цен» (ТЗ v2 §3.2): мин/средняя/макс по площадкам и где дешевле.

    Учитываются только известные цены (price>0); товары «цена по ссылке»
    считаются отдельно. Меньше двух цен — блок не показываем.
    """
    priced = [
        sp.product
        for sp in (list(response.scored) + list(response.near_miss))
        if sp.product.price > 0
    ]
    no_price = sum(
        1
        for sp in (list(response.scored) + list(response.near_miss))
        if sp.product.price <= 0
    )
    if len(priced) < 2:
        if no_price:
            return (
                f"\n💰 Цены в сниппетах не видны — смотрите по ссылкам "
                f"({no_price} тов. без цены в выдаче)."
            )
        return ""
    prices = sorted(p.price for p in priced)
    lo, avg, hi = prices[0], sum(prices) / len(prices), prices[-1]
    lo_p = min(priced, key=lambda p: p.price)
    lo_mp = MP_LABELS.get(getattr(lo_p, "marketplace", "ozon"), "Ozon")
    lines = [
        f"\n💰 *Разброс цен:* от {format_price(lo)} ({lo_mp}) · "
        f"средняя ~{format_price(avg)} · до {format_price(hi)}"
    ]
    if hi != lo:
        lines.append(f"Дешевле всего — {lo_mp}.")
    if no_price:
        lines.append(
            f"Ещё у {no_price} тов. цена по ссылке (в сниппетах не видна)."
        )
    return "\n".join(lines)


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
    mp_label = {"ozon": "Ozon", "wb": "WB", "yam": "ЯМ"}
    for i, sp in enumerate(chunk, start=start + 1):
        p = sp.product
        mp = mp_label.get(getattr(p, "marketplace", "ozon"), "Ozon")
        price = format_price(p.price) if p.price > 0 else "цена по ссылке"
        lines.append(
            f"{i}. [{mp}] *{_md(p.name)}* · SCORE *{sp.score}*\n"
            f"   {price} · [ссылка]({p.url})"
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

    price_line = (
        f"💰 {format_price(p.price)}"
        if p.price > 0
        else "💰 Цена по ссылке"
    )
    mp_label = {"ozon": "Ozon", "wb": "Wildberries", "yam": "Я.Маркет"}.get(
        getattr(p, "marketplace", "ozon"), "Ozon"
    )
    if p.price <= 0:
        link_line = f"{mp_label}: [открыть товар]({p.url})"
    else:
        link_line = f"[{mp_label}]({p.url})"
    parts = [
        f"{medal} *{_md(p.name)}* · ⭐ SCORE *{sp.score} / 100* {badge}",
        score_line,
        f"{price_line} | Рейтинг товара {rating_line}",
        f"🏪 Продавец: {_md(p.seller_name or '—')}{seller_rating}{orders}",
        f"🚚 Доставка: {delivery}",
        fbo,
        link_line,
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

