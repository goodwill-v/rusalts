"""Точка входа Telegram-бота Купец."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# корень проекта в PYTHONPATH
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage

from bot.handlers import router
from config import Config
from parser.healthcheck import run_healthcheck
from utils import get_logger, setup_logging


async def main() -> None:
    setup_logging()
    log = get_logger("bot.main")

    if not Config.TELEGRAM_BOT_TOKEN or set(Config.TELEGRAM_BOT_TOKEN) <= {"_"}:
        log.error("missing_telegram_token", hint="заполни TELEGRAM_BOT_TOKEN в .env")
        return

    # Тариф витрины Платформы АЛТ (унификация A6/A7): кеш 60 с, fail-open.
    # Лимиты применяются в handlers при наличии полей в тарифе kupec (админка).
    try:
        import vitrina_client

        _v = vitrina_client.VitrinaClient("kupec")
        _t = _v.get_tariff()
        log.info("vitrina_tariff", product="kupec",
                 price_rub=_t.get("price_rub"), trial_days=_t.get("trial_days"),
                 allowance_tokens=_t.get("allowance_tokens") or None,
                 source="vitrina" if _t else "fallback")
    except Exception as e:
        log.warning("vitrina_tariff_unavailable", error=str(e)[:120])

    log.info(
        "starting",
        dry_run=Config.DRY_RUN,
        log_level=Config.LOG_LEVEL,
        vision=Config.VISION_ENABLED,
    )

    ok = await run_healthcheck()
    if not ok:
        log.warning("parser_healthcheck_failed_continue")

    bot = Bot(
        token=Config.TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    log.info("polling")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
