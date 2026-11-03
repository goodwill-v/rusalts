"""HTTP-клиент Ozon с retry, semaphore и ротацией UA."""

from __future__ import annotations

import asyncio

import httpx

from config import Config
from utils import browser_headers, get_logger

logger = get_logger("parser.client")


class OzonHTTPClient:
    def __init__(self) -> None:
        self._sem = asyncio.Semaphore(Config.PARSING_CONCURRENCY)
        kwargs: dict = {
            "timeout": httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0),
            "follow_redirects": True,
            "headers": browser_headers(),
        }
        if Config.PARSING_USE_PROXY and Config.PARSING_PROXY_URL:
            kwargs["proxy"] = Config.PARSING_PROXY_URL
        self.session = httpx.AsyncClient(**kwargs)

    async def fetch(self, url: str) -> str | None:
        async with self._sem:
            delay = Config.PARSING_DELAY_MS / 1000.0
            if delay > 0:
                await asyncio.sleep(delay)
            for attempt in range(Config.PARSING_MAX_RETRIES):
                try:
                    self.session.headers.update(browser_headers())
                    resp = await self.session.get(url)
                    if resp.status_code == 200:
                        return resp.text
                    if resp.status_code == 429:
                        wait = 5 * (attempt + 1)
                        logger.warning("rate_limited", url=url, wait=wait)
                        await asyncio.sleep(wait)
                    else:
                        logger.warning(
                            "http_error", url=url, status=resp.status_code
                        )
                        return None
                except Exception as exc:
                    logger.warning("fetch_error", url=url, error=str(exc))
                    await asyncio.sleep(2 ** attempt)
            return None

    async def aclose(self) -> None:
        await self.session.aclose()
