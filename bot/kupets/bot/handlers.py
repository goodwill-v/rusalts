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
    quota_exhausted_message,
    start_message,
)
from bot.states import SearchStates
from engine.query_guard import is_smalltalk
from models.result import SearchFilters, SearchResponse
from services import quotas
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
