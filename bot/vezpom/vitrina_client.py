"""vitrina_client.py — СИНХРОННЫЙ клиент к API витрины Платформы АЛТ (для ВежПом).

ВежПом — sync-webhook (long polling), поэтому urllib вместо httpx.
- get_tariff(): актуальный тариф из админки (TTL 60 с + last-known fallback)
- check_subscription(): статус подписки на витрине по chat_id (для будущего
  переноса оплаты; сейчас у бота локальная подписка)
Правила шаблона TZ_Monetization_Unified_Block: X-API-Key, fail-open.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

TARIFF_TTL_SECONDS = 60.0
_TIMEOUT = 10

_tariff_cache: dict = {"at": 0.0, "value": None}
_plans_cache: dict = {"at": 0.0, "value": []}


def _base_url() -> str:
    return (os.environ.get("VITRINA_BASE_URL", "https://rusalts.ru") or "").rstrip("/")


def _api_key() -> str:
    return (os.environ.get("VITRINA_API_KEY", "") or "").strip()


def _get_raw(path: str) -> tuple[int | None, dict | None]:
    """GET с X-API-Key. Возвращает (http_status_or_None, data).
    status=None — сетевая ошибка/таймаут; 404 — подтверждённое отсутствие."""
    url = _base_url() + path
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "X-API-Key": _api_key(),
    })
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        logger.warning("Vitrina GET %s failed: HTTP %s", path, e.code)
        return e.code, None
    except Exception as e:  # сеть/таймаут — не роняем webhook
        logger.warning("Vitrina GET %s failed: %s", path, e)
        return None, None


def _get(path: str) -> dict | None:
    """GET с X-API-Key; None при любой ошибке (fail-open)."""
    _, data = _get_raw(path)
    return data


def get_tariff() -> dict:
    """Тариф продукта vezhpom: trial_days, period_days, price_rub, ...

    Возвращает dict (возможно пустой при недоступности — тогда вызывающий
    код использует локальные константы-фолбэки).
    """
    now = time.monotonic()
    if _tariff_cache["value"] is not None and now - _tariff_cache["at"] < TARIFF_TTL_SECONDS:
        return _tariff_cache["value"]
    data = _get("/api/tariff/vezhpom")
    if data and "trial_days" in data:
        _tariff_cache["at"] = now
        _tariff_cache["value"] = data
        return data
    if _tariff_cache["value"] is not None:
        return _tariff_cache["value"]  # последнее известное
    return {}


def get_plans(product_id: str = "vezhpom") -> list[dict]:
    """Список тарифных планов продукта (/api/tariffs/<id>), кэш TTL 60 с.

    [{plan, name, price_rub, period_days, trial_days, resource_limit}].
    [] — витрина недоступна (вызывающий использует фолбэк-лимиты).
    """
    now = time.monotonic()
    if _plans_cache["value"] and now - _plans_cache["at"] < TARIFF_TTL_SECONDS:
        return _plans_cache["value"]
    data = _get("/api/tariffs/" + product_id)
    plans = (data or {}).get("plans") or []
    if plans:
        _plans_cache.update(at=now, value=plans)
    return _plans_cache["value"]


def get_trial_days(default: int = 14) -> int:
    try:
        v = int(get_tariff().get("trial_days") or 0)
        return v if v > 0 else default
    except Exception:
        return default


def get_period_days(default: int = 30) -> int:
    try:
        v = int(get_tariff().get("period_days") or 0)
        return v if v > 0 else default
    except Exception:
        return default


def _post(path: str, payload: dict) -> dict | None:
    """POST с X-API-Key; None при любой ошибке (fail-open)."""
    url = _base_url() + path
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-API-Key": _api_key(),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        logger.warning("Vitrina POST %s failed: %s", path, e)
        return None
    except Exception as e:
        logger.warning("Vitrina POST %s failed: %s", path, e)
        return None


def check_subscription(chat_id: int) -> bool:
    """Активна ли подписка на витрине (customer_id = chat_id MAX).

    Подтверждённое отсутствие (404) -> False.
    Сетевой сбой -> True (fail-open, не блокируем мониторинг).
    """
    status, data = _get_raw(f"/api/subscription/{chat_id}/vezhpom")
    if status == 404:
        return False
    if data is None:
        return True  # сбой сети
    return data.get("status") == "active"


def get_subscription_info(chat_id: int) -> dict:
    """Информация о подписке с витрины: status, expires_at, остаток.

    {} при подтверждённом отсутствии или недоступности витрины.
    """
    status, data = _get_raw(f"/api/subscription/{chat_id}/vezhpom")
    return data or {}


def create_payment(chat_id: int, save_payment_method: bool = True) -> dict:
    """Создать платёж на витрине за vezhpom.

    Возвращает {confirmation_url, payment_id, status} или {} при ошибке.
    """
    data = _post("/api/create-payment", {
        "product_id": "vezhpom",
        "customer_id": str(chat_id),
        "save_payment_method": save_payment_method,
        "description": "ВежПом подписка — 150 руб/мес",
        "return_url": "https://rusalts.ru/techologis/?payment=success",
    }) or {}
    return {
        "confirmation_url": data.get("confirmation_url", ""),
        "payment_id": data.get("payment_id", ""),
        "status": data.get("status", ""),
    }


def renew_subscription(chat_id: int) -> dict:
    """Автопродление по сохранённой карте через витрину.

    Возвращает {ok, status, error?}.
    """
    data = _post("/api/subscription/renew", {
        "customer_id": str(chat_id),
        "product_id": "vezhpom",
    }) or {}
    return data


def refund_payment(payment_id: str, amount_rub: float | None = None) -> dict:
    """Возврат платежа через витрину."""
    payload: dict = {"payment_id": payment_id}
    if amount_rub is not None:
        payload["amount_rub"] = amount_rub
    data = _post("/api/payment/refund", payload) or {}
    return data


def delete_payment_method(chat_id: int) -> dict:
    """Отвязать сохранённую карту через витрину."""
    data = _post("/api/payment-method/delete", {
        "customer_id": str(chat_id),
        "product_id": "vezhpom",
    }) or {}
    return data
