"""vitrina_client.py — ЭТАЛОННЫЙ синхронный клиент к API витрины Платформы АЛТ.

Единый шаблон для всех ботов/продуктов (TZ_Monetization_Unified_Block + правки
ZAI, утв. 21–22.10). Копируется в каталог бота как есть; асинхронные боты
(ItaLidia) используют свой async-вариант с тем же контрактом.

Контракт:
- get_tariff() → dict из GET /api/tariff/{product_id} (X-API-Key)
- TTL-кеш 60 с ТОЛЬКО для тарифа (подписки — без кеша)
- timeout 5 с, 2 ретрая
- fail-open: сбой → последнее известное → пустой dict (вызывающий код
  применяет свои фолбэк-константы; для новых пользователей — триальный режим,
  не полный доступ)

Маппинг лимитов запросов (для ботов, ограничивающих запросы, а не токены):
    requests_limit(est_tokens_per_request=2000) → allowance_tokens // est
    trial_requests_limit(...)                    → trial_tokens // est
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.request

logger = logging.getLogger(__name__)

TARIFF_TTL_SECONDS = 60.0
REQUEST_TIMEOUT = 5.0
RETRIES = 2
EST_TOKENS_PER_REQUEST = 2000  # оценка для пересчёта токенов в запросы


class VitrinaClient:
    def __init__(self, product_id: str,
                 base_url: str | None = None, api_key: str | None = None):
        self.product_id = product_id
        self.base_url = (base_url or os.environ.get("VITRINA_BASE_URL", "https://rusalts.ru")).rstrip("/")
        self.api_key = (api_key or os.environ.get("VITRINA_API_KEY", "")).strip()
        self._cache: dict = {"at": 0.0, "value": None}

    def _get(self, path: str, retries: int = RETRIES) -> dict | None:
        last_err = None
        for attempt in range(retries + 1):
            req = urllib.request.Request(
                self.base_url + path,
                headers={"Accept": "application/json", "X-API-Key": self.api_key},
            )
            try:
                with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except Exception as e:
                last_err = e
                if attempt < retries:
                    time.sleep(0.3 * (attempt + 1))
        logger.warning("Vitrina GET %s failed after %d tries: %s", path, retries + 1, last_err)
        return None

    def get_tariff(self) -> dict:
        """Тариф продукта: TTL 60 с; сбой → last known → {} (фолбэк вызывающего)."""
        now = time.monotonic()
        if self._cache["value"] is not None and now - self._cache["at"] < TARIFF_TTL_SECONDS:
            return self._cache["value"]
        data = self._get(f"/api/tariff/{self.product_id}")
        if data and "error" not in data and ("price_rub" in data or "trial_days" in data):
            self._cache["at"] = now
            self._cache["value"] = data
            return data
        if self._cache["value"] is not None:
            return self._cache["value"]
        return {}

    def check_subscription(self, user_id: int | str) -> bool:
        """Активна ли подписка на витрине. fail-open: сбой → True (решает локальная логика)."""
        data = self._get(f"/api/subscription/{user_id}/{self.product_id}")
        if data is None:
            return True
        return data.get("status") == "active"

    # ─── Пересчёт в запросы (для rate-limit ботов) ──────────────────────

    def requests_limit(self, default: int, est: int = EST_TOKENS_PER_REQUEST) -> int:
        try:
            v = int(self.get_tariff().get("allowance_tokens") or 0)
            return max(1, v // est) if v > 0 else default
        except Exception:
            return default

    def trial_requests_limit(self, default: int, est: int = EST_TOKENS_PER_REQUEST) -> int:
        try:
            v = int(self.get_tariff().get("trial_tokens") or 0)
            return max(1, v // est) if v > 0 else default
        except Exception:
            return default
