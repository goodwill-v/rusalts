"""Sandbox router: payment API endpoints and the new techologis-v2 showcase page."""

from __future__ import annotations

import json
import secrets

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from datetime import datetime

from app import config
from app.billing import (
    TARIFFS,
    Tariff,
    get_or_create_balance,
    get_balance,
    get_remaining_tokens,
    get_subscription_text,
    format_token_allowance,
    is_balance_low,
    get_usage_summary,
    get_expired_subscriptions,
    deactivate_expired_subscriptions,
    top_up_balance,
)
from app.payments import PRODUCTS, create_payment, record_payment, update_payment_status

router = APIRouter()
templates = Jinja2Templates(directory=str(config.BASE_DIR / "app" / "templates"))


# ---------------------------------------------------------------------------
# Showcase page (v2 — new design)
# ---------------------------------------------------------------------------

@router.get("/techologis-v2/", response_class=HTMLResponse)
async def techologis_v2(request: Request, payment: str = "") -> HTMLResponse:
    """Новая витрина с каталогом продуктов и услуг."""
    products_list = [
        p for p in PRODUCTS.values() if p["category"] == "product" and p["status"] != "dev"
    ]
    services_list = [
        p for p in PRODUCTS.values() if p["category"] == "service" or p.get("status") == "dev"
    ]

    # Добавляем тарифы и тексты подписок в контекст шаблона
    tariffs_info = {}
    for pid, t in TARIFFS.items():
        tariffs_info[pid] = {
            "token_allowance": format_token_allowance(t.token_allowance),
            "estimated_dialogues": t.estimated_dialogues,
            "model": t.routerai_model_id,
            "trial_tokens": format_token_allowance(t.trial_tokens) if t.trial_tokens else "нет",
            "subscription_text": get_subscription_text(pid),
        }

    return templates.TemplateResponse(
        request,
        "techologis_v2.html",
        {
            "vk_app_id": config.VK_APP_ID or None,
            "is_widget": False,
            "layout_class": "layout-site",
            "page_title": "Магазин — АЛЬТЕРНАТИВА (АЛТ)",
            "products": products_list,
            "services": services_list,
            "payment_status": payment,
            "is_sandbox": config.IS_YOOKASSA_SANDBOX,
            "tariffs_info": tariffs_info,
            "billing_enabled": True,
        },
    )


# ---------------------------------------------------------------------------
# Payment API
# ---------------------------------------------------------------------------

@router.post("/api/create-payment")
async def api_create_payment(request: Request):
    """Create a ЮKassa payment for a product."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    product_id = data.get("product_id", "").strip()
    if product_id not in PRODUCTS:
        return JSONResponse({"error": f"Unknown product: {product_id}"}, status_code=404)

    prod = PRODUCTS[product_id]
    if prod["status"] in ("free", "inquiry", "coming", "dev"):
        return JSONResponse(
            {"error": f"Product '{prod['name']}' is not available for purchase yet. Status: {prod['status']}"},
            status_code=400,
        )

    amount_rub = data.get("amount_rub", 0) or prod["price_rub"]
    # return_url нужен только боевому режиму ЮKassa; берём адрес текущего запроса,
    # чтобы не зависеть от PUBLIC_BASE_URL (staging/IP/туннель).
    base = str(request.base_url).rstrip("/")
    return_url = data.get("return_url", "") or f"{base}/techologis-v2/?payment=success"
    description = data.get("description", "") or f"Оплата: {prod['name']}"

    try:
        result = await create_payment(
            product_id=product_id,
            amount_rub=amount_rub,
            description=description,
            return_url=return_url,
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

    # Record in local DB
    pm_id = result.get("payment_method", {}).get("id", "")
    record_payment(
        yookassa_id=result["id"],
        product_id=product_id,
        amount_rub=amount_rub,
        status=result.get("status", "pending"),
        payment_method_id=pm_id,
    )

    return JSONResponse({
        "payment_id": result["id"],
        "status": result.get("status", "pending"),
        "confirmation_url": result.get("confirmation", {}).get("confirmation_url", ""),
        "test": result.get("test", False),
    })


@router.get("/api/payment-demo")
async def payment_demo_redirect(
    yookassa_id: str = "",
    product_id: str = "",
):
    """Sandbox: simulate successful payment redirect."""
    if yookassa_id and yookassa_id.startswith("sandbox-"):
        update_payment_status(yookassa_id, "succeeded")
    # Относительный редирект: возвращаем туда, откуда пришли (staging, IP:порт, туннель).
    return RedirectResponse(url="/techologis-v2/?payment=success")


@router.post("/api/payment-webhook")
async def payment_webhook(request: Request):
    """ЮKassa webhook: handle payment.succeeded etc."""
    body_bytes = await request.body()
    signature = request.headers.get("Authorization", "")
    if not config.payments.verify_webhook_signature(body_bytes, signature):
        return JSONResponse({"error": "Invalid signature"}, status_code=401)

    event = json.loads(body_bytes)
    event_type = event.get("event", "")
    payment_obj = event.get("object", {})

    if event_type == "payment.succeeded":
        payment_id = payment_obj.get("id", "")
        pm_id = payment_obj.get("payment_method", {}).get("id", "")
        update_payment_status(payment_id, "succeeded", payment_method_id=pm_id)
        # TODO: activate subscription and notify user

    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Subscription Management (token-based billing)
# ---------------------------------------------------------------------------

@router.post("/api/activate-subscription")
async def api_activate_subscription(request: Request):
    """Активировать подписку после успешного платежа.
    Создаёт или продлевает баланс токенов для клиента."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    customer_id = data.get("customer_id", "").strip()
    product_id = data.get("product_id", "").strip()
    if not customer_id:
        return JSONResponse({"error": "customer_id is required"}, status_code=400)
    if product_id not in TARIFFS:
        return JSONResponse({"error": f"Unknown product: {product_id}"}, status_code=404)

    tariff = TARIFFS[product_id]
    allowance = tariff.token_allowance

    # Проверяем, есть ли активная подписка
    existing = get_balance(customer_id, product_id)
    if existing:
        # Продление: добавляем токены к текущему остатку
        # (в production: создать новую subscription_id)
        bal = get_or_create_balance(
            customer_id=customer_id,
            product_id=product_id,
            allowance_tokens=allowance,
            subscription_id=existing.get("subscription_id", 0) + 1,
            period_days=tariff.period_days if hasattr(tariff, "period_days") else 30,
        )
    else:
        bal = get_or_create_balance(
            customer_id=customer_id,
            product_id=product_id,
            allowance_tokens=allowance,
        )

    return JSONResponse({
        "status": "activated",
        "customer_id": customer_id,
        "product_id": product_id,
        "allowance_tokens": allowance,
        "remaining_tokens": get_remaining_tokens(customer_id, product_id),
        "expires_at": bal.get("expires_at", ""),
    })


@router.get("/api/subscription/{customer_id}/{product_id}")
async def api_get_subscription(customer_id: str, product_id: str):
    """Получить статус подписки клиента."""
    bal = get_balance(customer_id, product_id)
    if bal is None:
        return JSONResponse({"status": "no_subscription"}, status_code=404)

    remaining = get_remaining_tokens(customer_id, product_id)
    low = is_balance_low(customer_id, product_id)
    usage_stats = get_usage_summary(customer_id)

    return JSONResponse({
        "customer_id": customer_id,
        "product_id": product_id,
        "status": bal["status"],
        "allowance_tokens": bal["allowance_tokens"],
        "used_tokens": bal["used_tokens"],
        "remaining_tokens": remaining,
        "percent_used": round(bal["used_tokens"] / bal["allowance_tokens"] * 100, 1) if bal["allowance_tokens"] else 0,
        "balance_low": low,
        "expires_at": bal["expires_at"],
        "usage_summary": usage_stats,
    })


@router.get("/techologis-v2/subscription/")
async def subscription_page(request: Request, customer_id: str = "", product_id: str = ""):
    """Страница статуса подписки для клиента."""
    sub_info = None
    tariff_info = None
    error = None

    if customer_id and product_id:
        bal = get_balance(customer_id, product_id)
        if bal is None:
            error = "Подписка не найдена. Возможно, срок истёк."
        else:
            remaining = get_remaining_tokens(customer_id, product_id)
            tariff = TARIFFS.get(product_id)
            if tariff:
                tariff_info = {
                    "name": tariff.name,
                    "allowance": format_token_allowance(tariff.token_allowance),
                    "price": tariff.price_rub,
                }
            sub_info = {
                "customer_id": customer_id,
                "product_id": product_id,
                "status": bal["status"],
                "allowance_tokens": bal["allowance_tokens"],
                "used_tokens": bal["used_tokens"],
                "remaining_tokens": remaining,
                "expires_at": bal["expires_at"][:10],
                "balance_low": is_balance_low(customer_id, product_id),
            }

    return templates.TemplateResponse(
        request,
        "techologis_v2.html",
        {
            "vk_app_id": config.VK_APP_ID or None,
            "is_widget": False,
            "layout_class": "layout-site",
            "page_title": "Подписка — АЛТ",
            "products": [],
            "services": [],
            "payment_status": "",
            "is_sandbox": config.IS_YOOKASSA_SANDBOX,
            "subscription": sub_info,
            "subscription_error": error,
            "tariff_info": tariff_info,
            "billing_enabled": True,
        },
    )


@router.post("/api/subscription/top-up")
async def api_top_up_tokens(request: Request):
    """Докупка токенов к существующей подписке."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    customer_id = data.get("customer_id", "").strip()
    product_id = data.get("product_id", "").strip()
    extra_tokens = int(data.get("extra_tokens", 0))

    if not customer_id or not product_id or extra_tokens <= 0:
        return JSONResponse({"error": "customer_id, product_id and extra_tokens > 0 required"}, status_code=400)

    tariff = TARIFFS.get(product_id)
    if not tariff:
        return JSONResponse({"error": "Unknown product"}, status_code=404)

    # Стоимость докупки: extra_tokens × цена за токен
    avg_price_per_token = (tariff.routerai_prompt_price_per_1m + tariff.routerai_completion_price_per_1m) / 2 / 1_000_000
    top_up_cost = round(extra_tokens * avg_price_per_token * 1.2, 2)  # +20% наценка

    # Для sandbox: сразу зачисляем
    if config.IS_YOOKASSA_SANDBOX:
        new_allowance = top_up_balance(customer_id, product_id, extra_tokens)
        if new_allowance is None:
            return JSONResponse({"error": "Нет активной подписки для пополнения"}, status_code=404)

    return JSONResponse({
        "status": "top_up_initiated",
        "product_id": product_id,
        "extra_tokens": extra_tokens,
        "top_up_cost_rub": top_up_cost,
        "note": "В sandbox-режиме токены зачислены сразу. В production — через ЮKassa.",
    })


@router.post("/api/cron/check-expired")
async def cron_check_expired():
    """Cron-задача: деактивировать просроченные подписки.
    Вызывать раз в день (например, 03:00 UTC)."""
    expired = get_expired_subscriptions()
    deactivated = deactivate_expired_subscriptions()
    return JSONResponse({
        "found_expired": len(expired),
        "deactivated": deactivated,
    })