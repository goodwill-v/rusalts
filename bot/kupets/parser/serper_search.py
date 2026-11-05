"""Serper.dev — второй канал поиска (резерв/перелив Firecrawl).

Google-SERP по нашим запросам (site: OR работает), сниппеты WB/Ozon/ЯМ
с ценами и «оценками» — качество проверено живым тестом 06.11 с сервера АЛТ
(HTTP 200; Tavily тем же маршрутом заблокирована WAF).

Квота: стартовые 2 500 запросов Serper (SERPER_MONTHLY_QUERIES), счётчик —
в общем SQLite-хранилище кредитов (provider='serper'), при исчерпании —
ProviderQuotaError (канальный фолбэк решает, что дальше).
"""

from __future__ import annotations

import httpx

from config import Config
from services import credit_monitor, sqlite_store
from utils import get_logger

logger = get_logger("parser.serper")

SERPER_BASE = "https://google.serper.dev/search"
SERPER_TIMEOUT = 30


class SerperQuotaError(Exception):
    """Месячная квота Serper (SERPER_MONTHLY_QUERIES) исчерпана."""


class SerperClient:
    def __init__(self) -> None:
        self._api_key = Config.SERPER_API_KEY
        self._http = None

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    async def _ensure(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(SERPER_TIMEOUT + 10),
                headers={
                    "X-API-KEY": self._api_key,
                    "Content-Type": "application/json",
                },
            )
        return self._http

    def _quota_left(self) -> int:
        limit = max(0, Config.SERPER_MONTHLY_QUERIES)
        spent = sqlite_store.credits_spent("serper")
        return limit - int(spent)

    async def search_organic(self, query: str, num: int = 10) -> list[dict]:
        """Google-SERP: [{title, link, snippet, ...}] для запроса (site: в q).

        Квота проверяется до вызова; каждый успех — +1 к счётчику serper.
        """
        if not self.available:
            raise SerperQuotaError("Serper: ключ не настроен (SERPER_API_KEY)")
        if self._quota_left() <= 0:
            logger.error("serper_quota_exhausted")
            raise SerperQuotaError("Serper: месячная квота запросов исчерпана")
        http = await self._ensure()
        try:
            resp = await http.post(
                SERPER_BASE,
                json={"q": query, "num": max(1, min(int(num), 20))},
            )
            if resp.status_code in (401, 403):
                logger.error("serper_auth_fail", extra={"status": resp.status_code})
                raise SerperQuotaError("Serper: ключ отклонён (401/403)")
            if resp.status_code == 429:
                logger.error("serper_rate_limited")
                raise SerperQuotaError("Serper: лимит запросов (429)")
            if resp.status_code != 200:
                logger.error("serper_fail", extra={"status": resp.status_code})
                return []
            data = resp.json()
            organic = data.get("organic", []) or []
            if organic and not Config.DRY_RUN:
                credit_monitor.record_search_credits("serper", 1)
            return organic
        except httpx.TimeoutException:
            logger.error("serper_timeout")
            return []
        except SerperQuotaError:
            raise
        except Exception as exc:
            logger.error("serper_exc", extra={"error": str(exc)})
            return []

    async def aclose(self):
        if self._http is not None:
            await self._http.aclose()
            self._http = None
