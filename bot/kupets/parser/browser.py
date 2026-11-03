"""Опциональное обогащение через Playwright (обход 403 на /product/).

Включается флагом USE_BROWSER=true. Если playwright не установлен —
модуль тихо отключается.
"""

from __future__ import annotations

import asyncio
from typing import Any

from config import Config
from utils import get_logger

logger = get_logger("parser.browser")

_browser = None
_context = None
_lock = asyncio.Lock()


def browser_available() -> bool:
    if not Config.USE_BROWSER:
        return False
    try:
        import playwright  # noqa: F401

        return True
    except ImportError:
        return False


async def fetch_product_json(product_path: str) -> dict[str, Any] | None:
    """Загружает composer JSON со страницы товара через браузерную сессию."""
    if not browser_available():
        return None

    async with _lock:
        try:
            page = await _get_page()
            # прогрев
            await page.goto("https://www.ozon.ru/", wait_until="domcontentloaded", timeout=25000)
            api_url = (
                "https://api.ozon.ru/composer-api.bx/page/json/v2?url="
                + product_path
            )
            # encode via page.evaluate fetch with credentials
            data = await page.evaluate(
                """async (path) => {
                    const url = 'https://api.ozon.ru/composer-api.bx/page/json/v2?url='
                        + encodeURIComponent(path);
                    const r = await fetch(url, {credentials: 'include'});
                    if (!r.ok) return null;
                    return await r.json();
                }""",
                product_path,
            )
            if isinstance(data, dict) and data.get("widgetStates"):
                logger.info("browser_product_ok", path=product_path[:60])
                return data
            logger.warning("browser_product_empty", path=product_path[:60])
            return None
        except Exception as exc:
            logger.warning("browser_product_error", error=str(exc))
            return None


async def _get_page():
    global _browser, _context
    from playwright.async_api import async_playwright

    if _context is None:
        pw = await async_playwright().start()
        _browser = await pw.chromium.launch(headless=True)
        _context = await _browser.new_context(
            locale="ru-RU",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
        )
        logger.info("browser_started")
    return await _context.new_page()


async def shutdown_browser() -> None:
    global _browser, _context
    try:
        if _context:
            await _context.close()
        if _browser:
            await _browser.close()
    except Exception:
        pass
    _browser = None
    _context = None
