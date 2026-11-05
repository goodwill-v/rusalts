"""Хендлеры Telegram-бота."""

from __future__ import annotations

from copy import deepcopy

from aiogram import F, Router
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.keyboards import (
    build_cancel_report_keyboard,
    build_paging_keyboard,
    build_result_keyboard,
)
from bot.messages import (
    format_all_results,
    format_product_detail,
    format_reject_details,
    format_results,
    id_message,
    check_result_message,
    compare_result_message,
    feature_limit_message,
    gift_message,
    gift_usage_message,
    report_message,
    report_usage_message,
    quota_exhausted_message,
    start_message,
    watch_added_message,
    watch_disabled_message,
    watch_duplicate_message,
    watch_hint_message,
    watch_limit_message,
    watch_not_available_message,
    watch_not_found_message,
    watch_usage_message,
    watches_list_message,
)
from bot.states import SearchStates
from engine.query_guard import is_smalltalk
from models.result import SearchFilters, SearchResponse
from services import anticlone, compare, gift, quotas, report as report_svc, sqlite_store
from services.mailer import ErrorReport, send_error_report, smtp_configured
from services.search_pipeline import SearchPipeline
from config import Config
from utils import get_logger
from vision.recognizer import PhotoRecognizer
from vision.stub import photo_stub_message

logger = get_logger("bot.handlers")
router = Router(name="kupets")
_photo_recognizer = PhotoRecognizer()


@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(SearchStates.idle)
    await message.answer(start_message(), parse_mode=ParseMode.MARKDOWN)


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    """Ваш ID (стандарт ботов АЛТ): нужен для выдачи доступа в админке витрины."""
    await message.answer(id_message(message.from_user.id))


# ─── «Жду скидку»: слежка за ценой (Ф1) ──────────────────────────────
# Команды зарегистрированы ДО catch-all F.text-хендлера поиска.


FEATURE_LABELS = {
    "xvsy": "Сравнение",
    "check": "Проверка на подделку",
    "podarok": "Подарочный подбор",
    "report": "Сводка для продавцов",
}


async def _feature_gate(message: Message, feature: str, pool: int) -> bool:
    """Квота фичи + место в поисковом пуле. False — отказ уже отправлен."""
    user = message.from_user
    if not user:
        return False
    plan = await quotas.user_plan(user.id)
    allowed, info = quotas.check_feature_quota(user.id, plan, feature)
    if not allowed:
        await message.answer(
            feature_limit_message(FEATURE_LABELS.get(feature, feature), info)
        )
        return False
    # пул поисков: фича расходует pool поисков (X vs Y=2, check=1)
    pool_ok, pool_info = quotas.check_search_quota(user.id, plan)
    if not pool_ok or pool_info["limit"] - pool_info["used"] < pool:
        await message.answer(quota_exhausted_message({**pool_info, "period": pool_info.get("period")}))
        return False
    quotas.consume_feature(user.id, feature, plan)
    for _ in range(pool):
        quotas.consume_search(user.id, plan)
    return True


async def _run_compare(message: Message, qa: str, qb: str) -> None:
    progress = await message.answer(
        f"⚖️ Сравниваю: {qa} ↔ {qb} (два поиска из вашего пакета)…"
    )
    try:
        from parser.search_channel import search_marketplaces as channel_search

        items_a = await channel_search(qa, limit=10)
        items_b = await channel_search(qb, limit=10)
        best_a, best_b = compare.pick_best(items_a), compare.pick_best(items_b)
        a = {
            "name": best_a.name, "price": best_a.price, "rating": best_a.rating,
            "reviews_count": best_a.reviews_count,
            "marketplace": best_a.marketplace, "url": best_a.url,
        } if best_a else None
        b = {
            "name": best_b.name, "price": best_b.price, "rating": best_b.rating,
            "reviews_count": best_b.reviews_count,
            "marketplace": best_b.marketplace, "url": best_b.url,
        } if best_b else None
        computed = compare.compare_verdict(best_a, best_b)
        ai_verdict = computed
        try:
            from ai.explainer import AIExplainer

            ex = AIExplainer()
            if best_a and best_b:
                ai_verdict = await ex.explain_compare(
                    {
                        "name": best_a.name, "price": best_a.price,
                        "rating": best_a.rating,
                        "reviews": best_a.reviews_count,
                        "marketplace": best_a.marketplace,
                    },
                    {
                        "name": best_b.name, "price": best_b.price,
                        "rating": best_b.rating,
                        "reviews": best_b.reviews_count,
                        "marketplace": best_b.marketplace,
                    },
                )
                if not ai_verdict:
                    ai_verdict = computed
        except Exception as exc:
            logger.warning("compare_ai_fail", extra={"error": str(exc)})
            ai_verdict = computed
        text = compare_result_message(
            qa, qb, a, b, ai_verdict,
            "Сравнение потратило 2 поиска из вашего месячного пакета.",
        )
        await progress.edit_text(text, parse_mode="Markdown",
                                 disable_web_page_preview=True)
    except Exception as exc:
        logger.exception("compare_failed", extra={"error": str(exc)})
        await progress.edit_text(
            "⚠️ Не удалось сравнить — попробуйте чуть позже."
        )


async def _run_check(message: Message, name: str) -> None:
    progress = await message.answer(
        f"🛡 Проверяю «{name[:50]}» (один поиск из вашего пакета)…"
    )
    try:
        from parser.search_channel import search_marketplaces as channel_search

        items = await channel_search(name, limit=10)
        verdict = anticlone.verdict_for(name, items)
        await progress.edit_text(
            check_result_message(name, verdict), parse_mode="Markdown"
        )
    except Exception as exc:
        logger.exception("check_failed", extra={"error": str(exc)})
        await progress.edit_text(
            "⚠️ Не удалось проверить — попробуйте чуть позже."
        )


@router.message(Command("report"))
async def cmd_report(message: Message, state: FSMContext) -> None:
    await state.clear()
    niche = _command_arg(message.text or "").strip()
    if not niche or is_smalltalk(niche):
        await message.answer(report_usage_message())
        return
    user = message.from_user
    plan = await quotas.user_plan(user.id)
    allowed, info = quotas.check_feature_quota(user.id, plan, "report")
    if not allowed:
        await message.answer(
            feature_limit_message(FEATURE_LABELS["report"], info)
        )
        return
    quotas.consume_feature(user.id, "report", plan)
    progress = await message.answer(
        f"📊 Собираю сводку по нише «{niche}» (4 запроса, до минуты)…"
    )
    try:
        from ai.explainer import AIExplainer

        items, samples = await report_svc.collect_niche(niche)
        stats = report_svc.build_report(niche, items, price_samples=samples)
        ai_summary = ""
        try:
            ai_summary = await AIExplainer().summarize_report(
                report_svc.stats_for_ai(stats)
            )
        except Exception as exc:
            logger.warning("report_ai_fail", extra={"error": str(exc)})
        await progress.edit_text(
            report_message(stats, ai_summary), parse_mode="Markdown",
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logger.exception("report_failed", extra={"error": str(exc)})
        await progress.edit_text(
            "⚠️ Не удалось собрать сводку — попробуйте чуть позже."
        )


@router.message(Command("gift"))
async def cmd_gift(message: Message, state: FSMContext) -> None:
    await state.clear()
    parsed = gift.parse_gift_request(_command_arg(message.text or ""))
    if not parsed:
        await message.answer(gift_usage_message())
        return
    if not await _feature_gate(message, "podarok", pool=gift.max_searches()):
        return
    await _run_gift(message, parsed[0], parsed[1])


async def _run_gift(message: Message, description: str, budget: int) -> None:
    progress = await message.answer(
        f"🎁 Подбираю подарки: {description} до {budget}₽…"
    )
    try:
        from ai.explainer import AIExplainer
        from parser.search_channel import search_marketplaces as channel_search

        ex = AIExplainer()
        queries = await ex.expand_gift(description, budget, gift.max_searches())
        if not queries:
            await progress.edit_text(
                "😔 Не смог придумать варианты — попробуйте уточнить "
                "описание: /gift маме до 2000"
            )
            return
        picks: list[tuple[str, object]] = []
        for q in queries:
            items = await channel_search(q, limit=5)
            picks.append((q, compare.pick_best(items)))
        await progress.edit_text(
            gift_message(description, budget, picks),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logger.exception("gift_failed", extra={"error": str(exc)})
        await progress.edit_text(
            "⚠️ Не удалось собрать подбор — попробуйте чуть позже."
        )


@router.message(Command("compare"))
async def cmd_compare(message: Message, state: FSMContext) -> None:
    await state.clear()
    parsed = compare.parse_compare_query("сравни " + _command_arg(message.text or ""))
    if not parsed:
        await message.answer(
            "Укажите два товара через «vs» или «и»:\n"
            "/compare Redmi Note 14 vs Realme C75"
        )
        return
    if not await _feature_gate(message, "xvsy", pool=2):
        return
    await _run_compare(message, parsed[0], parsed[1])


@router.message(Command("check"))
async def cmd_check(message: Message, state: FSMContext) -> None:
    await state.clear()
    name = _command_arg(message.text or "").strip()
    if not name or is_smalltalk(name):
        await message.answer(
            "Укажите товар:\n/check I15 Pro Max 256гб\n"
            "/check чехол iPhone 15 оригинал"
        )
        return
    if not await _feature_gate(message, "check", pool=1):
        return
    await _run_check(message, name)


@router.message(Command("watch"))
async def cmd_watch(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _handle_watch_request(message, _command_arg(message.text or ""))


@router.message(Command("watches"))
async def cmd_watches(message: Message) -> None:
    await _send_watches_list(message)


@router.message(Command("unwatch"))
async def cmd_unwatch(message: Message) -> None:
    await _handle_unwatch(message, _command_arg(message.text or ""))


async def _handle_watch_request(message: Message, query: str) -> None:
    user = message.from_user
    if not user:
        return
    query = query.strip()
    if not query:
        await message.answer(watch_usage_message())
        return
    parsed_compare = compare.parse_compare_query(query)
    if parsed_compare:
        if not await _feature_gate(message, "xvsy", pool=2):
            return
        await _run_compare(message, parsed_compare[0], parsed_compare[1])
        return
    if query.lower().lstrip().startswith(("подбери подарок", "подбери подарки")):
        name = query.split(" ", 1)[1].strip() if " " in query else ""
        parsed_gift = gift.parse_gift_request(name)
        if parsed_gift:
            if not await _feature_gate(message, "podarok", pool=gift.max_searches()):
                return
            await _run_gift(message, parsed_gift[0], parsed_gift[1])
            return
    if query.lower().lstrip().startswith("проверь "):
        name = query.split(" ", 1)[1].strip()
        if name and not is_smalltalk(name):
            if not await _feature_gate(message, "check", pool=1):
                return
            await _run_check(message, name)
            return
    if is_smalltalk(query):
        await message.answer(
            "Это не похоже на товарный запрос. "
            "Например: /watch термос для школы до 2000"
        )
        return

    plan = await quotas.user_plan(user.id)
    allowed, info = quotas.check_watch_quota(user.id, plan)
    if not allowed:
        # free (лимит 0) — фича недоступна; kupec — исчерпан лимит 5
        await message.answer(
            watch_not_available_message()
            if plan == quotas.FREE_PLAN
            else watch_limit_message(info)
        )
        return

    watch_id = sqlite_store.watch_add(
        str(user.id), str(message.chat.id), query
    )
    if watch_id is None:
        await message.answer(watch_duplicate_message(query))
        return
    logger.info(
        "watch_added",
        extra={"user_id": user.id, "watch_id": watch_id, "plan": plan},
    )
    await message.answer(
        watch_added_message(query, info), parse_mode=ParseMode.MARKDOWN
    )


async def _send_watches_list(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    plan = await quotas.user_plan(user.id)
    watches = sqlite_store.watch_list(str(user.id))
    _, info = quotas.check_watch_quota(user.id, plan)
    await message.answer(
        watches_list_message(watches, info), parse_mode=ParseMode.MARKDOWN
    )


async def _handle_unwatch(message: Message, arg: str) -> None:
    user = message.from_user
    if not user:
        return
    try:
        watch_id = int(arg)
    except ValueError:
        await message.answer(watch_hint_message())
        return
    if sqlite_store.watch_deactivate(str(user.id), watch_id):
        watch = next(
            (
                w
                for w in sqlite_store.watch_list(
                    str(user.id), active_only=False
                )
                if w["id"] == watch_id
            ),
            None,
        )
        await message.answer(
            watch_disabled_message(watch or {"id": watch_id, "query": ""}),
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await message.answer(watch_not_found_message(watch_id))


def _command_arg(text: str) -> str:
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


@router.message(F.photo)
async def on_photo(message: Message, state: FSMContext) -> None:
    if not _photo_recognizer.available:
        await message.answer(photo_stub_message())
        return

    progress = await message.answer("📷 Смотрю на фото…")
    try:
        photo = message.photo[-1]
        file = await message.bot.download(photo)
        image_bytes = file.read()
    except Exception as exc:
        logger.warning("photo_download_failed", error=str(exc))
        await progress.edit_text(
            "⚠️ Не удалось скачать фото. Пришлите ещё раз или опишите товар текстом."
        )
        return

    # Telegram отдаёт jpeg; ограничим размер для API
    if len(image_bytes) > 8_000_000:
        await progress.edit_text(
            "⚠️ Фото слишком большое. Пришлите более лёгкий снимок."
        )
        return

    try:
        recognition = await _photo_recognizer.recognize(
            image_bytes,
            caption=message.caption,
        )
    except Exception as exc:
        logger.warning("photo_recognize_failed", error=str(exc))
        await progress.edit_text(
            "⚠️ Не распознал товар на фото. "
            "Попробуйте другое фото или напишите запрос текстом."
        )
        return

    query = recognition.query
    if not query:
        await progress.edit_text(photo_stub_message())
        return

    note_parts = [f"🔎 По фото ищу: *{_escape_md(query)}*"]
    if recognition.notes:
        note_parts.append(f"_({_escape_md(recognition.notes)})_")
    try:
        await progress.edit_text(
            "\n".join(note_parts),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        await progress.edit_text(f"🔎 По фото ищу: {query}")

    # Подпись «сводка»/«отчёт» на снимке → анализ ниши вместо обычного поиска
    caption = (message.caption or "").strip()
    if caption and should_report_caption(caption):
        user = message.from_user
        plan = await quotas.user_plan(user.id)
        allowed, finfo = quotas.check_feature_quota(user.id, plan, "report")
        if not allowed:
            await progress.edit_text(
                feature_limit_message(FEATURE_LABELS["report"], finfo)
            )
            return
        pool_ok, pool_info = quotas.check_search_quota(user.id, plan)
        if not pool_ok:
            await progress.edit_text(quota_exhausted_message(pool_info))
            return
        quotas.consume_feature(user.id, "report", plan)
        await progress.edit_text(f"📊 Сводка по снимку: анализирую нишу «{query}»…")
        items, samples = await report_svc.collect_niche(query)
        await progress.edit_text(
            report_message(report_svc.build_report(query, items, price_samples=samples)),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        return

    await _run_and_reply(
        message,
        state,
        query=query,
        progress_message=progress,
        user_id=message.from_user.id if message.from_user else None,
    )


@router.message(SearchStates.awaiting_error_report, F.text)
async def on_error_report_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Опишите, пожалуйста, текстом — что не так или какое улучшение нужно.")
        return
    if text.lower() in {"/cancel", "отмена", "cancel"}:
        await state.set_state(SearchStates.has_results)
        await message.answer("Отменено. Можно продолжить поиск.")
        return

    data = await state.get_data()
    user = message.from_user
    report = ErrorReport(
        user_id=user.id if user else None,
        username=user.username if user else None,
        full_name=user.full_name if user else None,
        chat_id=message.chat.id,
        text=text,
        last_query=data.get("last_query"),
    )
    logger.warning(
        "user_report_error",
        user_id=report.user_id,
        chat_id=report.chat_id,
        text=text[:200],
    )

    await message.answer("⏳ Отправляю сообщение…")
    ok = await send_error_report(report)
    await state.set_state(SearchStates.has_results)
    if ok:
        await message.answer(
            "✅ Спасибо! Ваше сообщение отправлено. "
            "Мы посмотрим и учтём в работе над сервисом."
        )
    else:
        await message.answer(
            "⚠️ Не удалось отправить письмо (почта не настроена или сбой SMTP). "
            "Сообщение сохранено в логах. Попробуй позже или напиши напрямую."
        )


@router.message(F.text)
async def on_text_search(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    if current == SearchStates.awaiting_error_report.state:
        return
    query = (message.text or "").strip()
    if not query or query.startswith("/"):
        return

    # Разговорные команды слежки — до guard болтовни и поиска
    lower = query.lower()
    if lower.startswith(("следи за", "следить за")):
        prefix = "следи за" if lower.startswith("следи за") else "следить за"
        await _handle_watch_request(message, query[len(prefix):])
        return
    if lower in {"мои слежки", "слежки", "мои подписки"}:
        await _send_watches_list(message)
        return
    if lower.startswith(
        ("перестань следить", "не следи", "отключи слежк", "убери слежк")
    ):
        await message.answer(watch_hint_message())
        return

    if is_smalltalk(query):
        await message.answer(
            "👋 Привет! Я Купец — ищу и сравниваю товары на Ozon, "
            "Wildberries и Яндекс.Маркете.\n\n"
            "Напишите, что искать, например:\n"
            "• «термос для школы до 2000 руб»\n"
            "• «найди андроид 8ГБ/128ГБ до 20000»\n"
            "• «чехол для iPhone 15»\n\n"
            "Или пришлите фото товара — найду похожий."
        )
        return
    await _run_and_reply(
        message,
        state,
        query=query,
        user_id=message.from_user.id if message.from_user else None,
    )


@router.callback_query(F.data == "new_search")
async def cb_new_search(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(SearchStates.idle)
    await callback.message.answer(start_message(), parse_mode=ParseMode.MARKDOWN)
    await callback.answer()


@router.callback_query(F.data.startswith("detail:"))
async def cb_detail(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    response = _deserialize_response(data.get("last_response"))
    if response is None or not response.scored:
        await callback.answer("Нет сохранённых результатов — сделай новый поиск")
        return
    try:
        idx = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Ошибка")
        return
    if idx < 0 or idx >= len(response.scored):
        await callback.answer("Нет такого товара")
        return
    text = format_product_detail(response.scored[idx], idx)
    await callback.message.answer(
        text,
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("show_all:"))
async def cb_show_all(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    response = _deserialize_response(data.get("last_response"))
    if response is None:
        await callback.answer("Нет результатов")
        return
    try:
        page = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        page = 0
    text = format_all_results(response, page=page)
    kb = build_paging_keyboard(page, len(response.scored))
    await callback.message.answer(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
        disable_web_page_preview=True,
    )
    await callback.answer()


@router.callback_query(F.data == "show_rejects")
async def cb_show_rejects(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    response = _deserialize_response(data.get("last_response"))
    if response is None:
        await callback.answer("Нет результатов")
        return
    text = format_reject_details(response)
    await callback.message.answer(text, parse_mode=ParseMode.MARKDOWN)
    await callback.answer()


@router.callback_query(F.data.startswith("refine:"))
async def cb_refine(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    response = _deserialize_response(data.get("last_response"))
    last_query = data.get("last_query") or ""
    if response is None:
        await callback.answer("Сначала сделай поиск")
        return

    parts = callback.data.split(":")
    kind = parts[1] if len(parts) > 1 else ""
    filters = deepcopy(response.filters)

    if kind == "fbo":
        filters.only_fbo = True
        label = "только FBO"
    elif kind == "price" and len(parts) > 2:
        try:
            filters.price_max = float(parts[2])
        except ValueError:
            await callback.answer("Ошибка цены")
            return
        label = f"до {int(filters.price_max)} ₽"
    elif kind == "brand" and len(parts) > 2:
        filters.brand = parts[2]
        label = f"бренд {filters.brand}"
    else:
        await callback.answer("Неизвестное уточнение")
        return

    await callback.answer(f"Уточняю: {label}")
    await _run_and_reply(
        callback.message,
        state,
        query=last_query or filters.text,
        filters_override=filters,
        skip_cache=True,
        user_id=callback.from_user.id if callback.from_user else None,
    )


@router.callback_query(F.data == "report_error")
async def cb_report(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SearchStates.awaiting_error_report)
    await callback.message.answer(
        "Опишите, пожалуйста, что не так "
        "(неверный товар, странный SCORE, ошибка поиска и т.п.).\n"
        "Опишите какие улучшения для сервиса вы бы хотели получить.\n"
        "Чтобы отменить — нажмите «Отмена» или напишите «отмена».",
        reply_markup=build_cancel_report_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "cancel_report")
async def cb_cancel_report(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SearchStates.has_results)
    await callback.message.answer("Отменено. Можно продолжить поиск.")
    await callback.answer()


async def _run_and_reply(
    message: Message,
    state: FSMContext,
    *,
    query: str,
    filters_override: SearchFilters | None = None,
    skip_cache: bool = False,
    progress_message: Message | None = None,
    user_id: int | None = None,
) -> None:
    # Старый circuit-gate снят: поиск не блокируется на «Ozon ограничивает
    # запросы» — живой канал Firecrawl работает всегда (лимиты ведёт
    # credit_monitor, у пользователя честное пояснение при исчерпании).
    progress = progress_message or await message.answer("🔍 Ищу по маркетплейсам...")

    # Квота тарифа: free — 3 поиска/сутки, «Покупатель» (вкл. триал) — 6.
    # Списываем за попытку (до пайплайна) — защита от параллельных запросов.
    if user_id is not None:
        plan = await quotas.user_plan(user_id)
        allowed, info = quotas.check_search_quota(user_id, plan)
        if not allowed:
            await progress.edit_text(quota_exhausted_message(info))
            return
        quotas.consume_search(user_id, plan)
    if progress_message:
        try:
            await progress.edit_text(f"🔍 Ищу на Ozon: {query}")
        except Exception:
            pass
    try:
        await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    except Exception:
        pass

    async def on_progress(text: str) -> None:
        try:
            await progress.edit_text(text)
        except Exception:
            pass
        try:
            await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        except Exception:
            pass

    pipeline = SearchPipeline()
    try:
        response = await pipeline.run(
            query,
            on_progress=on_progress,
            filters_override=filters_override,
            skip_cache=skip_cache,
        )
    except Exception as exc:
        logger.exception("search_failed", error=str(exc))
        await progress.edit_text(
            "⚠️ Не удалось выполнить поиск. Попробуй ещё раз чуть позже."
        )
        return

    text = format_results(response)
    keyboard = (
        build_result_keyboard(response)
        if response.scored or response.near_miss or response.rejected
        else None
    )

    await state.set_state(SearchStates.has_results)
    await state.update_data(
        last_response=_serialize_response(response),
        last_query=query,
    )

    try:
        await progress.edit_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except Exception:
        await progress.edit_text(
            text,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )


def _escape_md(text: str) -> str:
    return (
        text.replace("*", "∗")
        .replace("_", "＿")
        .replace("`", "'")
        .replace("[", "(")
        .replace("]", ")")
    )


def _serialize_response(response: SearchResponse) -> dict:
    def ser_sp(sp):
        p = sp.product
        return {
            "score": sp.score,
            "verdict": sp.verdict,
            "match_notes": sp.match_notes,
            "breakdown": sp.breakdown.__dict__,
            "near_miss_reasons": p.near_miss_reasons,
            "product": {
                "product_id": p.product_id,
                "name": p.name,
                "price": p.price,
                "url": p.url,
                "rating": p.rating,
                "reviews_count": p.reviews_count,
                "seller_name": p.seller_name,
                "delivery_type": p.delivery_type,
                "delivery_days": p.delivery_days,
                "delivery_price": p.delivery_price,
                "ram_gb": p.ram_gb,
                "storage_gb": p.storage_gb,
                "screen_inches": p.screen_inches,
                "seller_rating": p.seller.rating if p.seller else None,
                "orders_count": p.seller.orders_count if p.seller else None,
            },
        }

    return {
        "query_display": response.query_display,
        "found_total": response.found_total,
        "ozon_unavailable": response.ozon_unavailable,
        "ozon_retry_sec": response.ozon_retry_sec,
        "filters": response.filters.__dict__,
        "scored": [ser_sp(sp) for sp in response.scored],
        "near_miss": [ser_sp(sp) for sp in response.near_miss],
        "rejected": [
            {"name": p.name, "reasons": reasons}
            for p, reasons in response.rejected
        ],
        "verdicts": response.verdicts,
    }


def _deserialize_response(data: dict | None) -> SearchResponse | None:
    if not data:
        return None
    from models.product import ProductData, SellerData
    from models.result import ScoreBreakdown, ScoredProduct

    def de_sp(raw: dict) -> ScoredProduct:
        pd = raw["product"]
        seller = None
        if pd.get("seller_rating") is not None or pd.get("orders_count"):
            seller = SellerData(
                seller_id="",
                seller_name=pd.get("seller_name") or "",
                rating=pd.get("seller_rating"),
                orders_count=pd.get("orders_count"),
            )
        product = ProductData(
            product_id=pd["product_id"],
            name=pd["name"],
            price=pd["price"],
            url=pd["url"],
            rating=pd.get("rating"),
            reviews_count=pd.get("reviews_count") or 0,
            seller_name=pd.get("seller_name") or "",
            delivery_type=pd.get("delivery_type") or "FBS",
            delivery_days=pd.get("delivery_days"),
            delivery_price=pd.get("delivery_price") or 0,
            ram_gb=pd.get("ram_gb"),
            storage_gb=pd.get("storage_gb"),
            screen_inches=pd.get("screen_inches"),
            seller=seller,
            near_miss_reasons=raw.get("near_miss_reasons") or [],
        )
        bd = raw.get("breakdown") or {}
        return ScoredProduct(
            product=product,
            score=raw["score"],
            breakdown=ScoreBreakdown(
                **{
                    k: bd.get(k, 0)
                    for k in (
                        "seller",
                        "price",
                        "reviews",
                        "delivery",
                        "soft_penalty",
                    )
                }
            ),
            verdict=raw.get("verdict") or "",
            match_notes=raw.get("match_notes") or [],
        )

    raw_filters = dict(data["filters"])
    raw_filters.setdefault("only_fbo", False)
    filters = SearchFilters(**raw_filters)
    rejected = []
    for r in data.get("rejected") or []:
        rejected.append(
            (
                ProductData(
                    product_id="0",
                    name=r["name"],
                    price=0,
                    url="",
                ),
                r["reasons"],
            )
        )

    return SearchResponse(
        filters=filters,
        scored=[de_sp(x) for x in data.get("scored") or []],
        near_miss=[de_sp(x) for x in data.get("near_miss") or []],
        rejected=rejected,
        verdicts=data.get("verdicts") or [],
        found_total=data.get("found_total") or 0,
        query_display=data.get("query_display") or "",
        ozon_unavailable=bool(data.get("ozon_unavailable")),
        ozon_retry_sec=int(data.get("ozon_retry_sec") or 0),
    )
