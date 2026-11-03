"""Клиент Ozon Composer API (api.ozon.ru) через curl_cffi.

HTML www.ozon.ru и product-страницы часто отдают 403 (антибот).
Рабочий путь для поиска: composer-api.bx/page/json/v2 с TLS-impersonation Chrome.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import quote

from config import Config
from parser.circuit import ozon_circuit
from utils import get_logger

logger = get_logger("parser.composer")

COMPOSER_BASE = "https://api.ozon.ru/composer-api.bx/page/json/v2"


class ComposerClient:
    """Async-обёртка над синхронным curl_cffi (через to_thread)."""

    def __init__(self) -> None:
        self._headers = {
            "Accept": "application/json",
            "Accept-Language": "ru",
            "Referer": "https://www.ozon.ru/",
            "x-o3-app-name": "mweb_client",
            "x-o3-app-version": "release",
        }
        self._impersonate = "chrome124"
        self._session = None
        # Был ли последний неуспех из‑за антибота (403/429/challenge)
        self.last_blocked: bool = False

    def _ensure_session(self):
        if self._session is None:
            from curl_cffi import requests as crequests

            self._session = crequests.Session(impersonate=self._impersonate)
        return self._session

    async def fetch_page(
        self,
        page_path: str,
        *,
        attempts: int | None = None,
    ) -> dict[str, Any] | None:
        """page_path: '/search/?text=...' или '/product/123/'."""
        if ozon_circuit.is_open:
            self.last_blocked = True
            logger.warning(
                "circuit_blocked",
                remaining=ozon_circuit.remaining_sec(),
                path=page_path[:60],
            )
            return None

        if not page_path.startswith("/"):
            page_path = "/" + page_path
        url = f"{COMPOSER_BASE}?url={quote(page_path, safe='')}"
        delay = max(Config.PARSING_DELAY_MS / 1000.0, 0.3)
        retries = attempts if attempts is not None else Config.PARSING_MAX_RETRIES

        for attempt in range(retries):
            if attempt or delay:
                await asyncio.sleep(delay if attempt == 0 else (2 ** attempt))
            try:
                data = await asyncio.to_thread(self._get_json, url)
                if data is not None:
                    ozon_circuit.record_success()
                    self.last_blocked = False
                    return data
            except Exception as exc:
                logger.warning(
                    "composer_error",
                    path=page_path[:80],
                    attempt=attempt,
                    error=str(exc),
                )
                ozon_circuit.record_failure()
        return None

    def _get_json(self, url: str) -> dict[str, Any] | None:
        session = self._ensure_session()
        resp = session.get(url, headers=self._headers, timeout=30)
        if resp.status_code == 200:
            try:
                return resp.json()
            except json.JSONDecodeError:
                logger.warning("composer_bad_json", url=url[:100])
                return None
        body_preview = (resp.text or "")[:200]
        blocked = resp.status_code in {403, 429} or _looks_like_challenge(
            body_preview
        )
        if blocked:
            self.last_blocked = True
        logger.warning(
            "composer_http",
            status=resp.status_code,
            url=url[:100],
            body=body_preview[:100],
        )
        ozon_circuit.record_failure(resp.status_code)
        if resp.status_code in {403, 429}:
            try:
                session.close()
            except Exception:
                pass
            self._session = None
        return None

    async def aclose(self) -> None:
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = None


def _looks_like_challenge(body: str) -> bool:
    low = body.lower()
    return "challengeurl" in low or "fab_chlg" in low or "incidentid" in low