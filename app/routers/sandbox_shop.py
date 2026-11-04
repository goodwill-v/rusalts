"""Sandbox router: payment API endpoints for the techologis showcase page."""

from __future__ import annotations

import json
import secrets

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from datetime import datetime, timezone

from app import config
from app.billing import (
    TARIFFS,
    Tariff,
    check_before_inference,
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
    renew_balance,
)
from app.payments import (
    PRODUCTS,
    create_payment,
    create_recurring_payment,
    create_refund,
    delete_payment_method,
    get_payment,
    get_payment_method_id,
    get_payment_record,
    record_payment,
    update_payment_status,
)

router = APIRouter()
from app.templating import templates  # noqa: E402  — общий экземпляр с content_block


# ---------------------------------------------------------------------------
# Redirect from old URL
# ---------------------------------------------------------------------------

@router.get("/techologis-v2/")
async def techologis_v2_redirect():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/techologis/", status_code=301)


# ---------------------------------------------------------------------------
# Product pages
# ---------------------------------------------------------------------------

@router.get("/techologis/italidia/")
async def italidia_product_page(request: Request):
    """Страница подробнее о продукте ItaLidia."""
    from app.products_store import catalog

    prod = next((p for p in catalog() if p["id"] == "italidia"), {})
    return templates.TemplateResponse(
        request,
        "italidia_product.html",
        {
            "vk_app_id": config.VK_APP_ID or None,
            "is_widget": False,
            "layout_class": "layout-site",
            "page_title": "ItaLidia — ассистент итальянского языка — АЛТ",
            "bot_url": prod.get("bot_url", ""),
            "name": prod.get("name", "ItaLidia"),
        },
    )


@router.get("/techologis/neyrosotrudnik/")
async def neyrosotrudnik_product_page(request: Request):
    """Страница подробнее о продукте НейроСотрудник."""
    from app.products_store import catalog

    prod = next((p for p in catalog() if p["id"] == "neyrosotrudnik"), {})
    return templates.TemplateResponse(
        request,
        "neyrosotrudnik_product.html",
        {
            "vk_app_id": config.VK_APP_ID or None,
            "is_widget": False,
            "layout_class": "layout-site",
            "page_title": "НейроСотрудник — ИИ-ассистент в МАКС — АЛТ",
            "bot_url": prod.get("bot_url", ""),
            "name": prod.get("name", "НейроСотрудник"),
        },
    )


@router.get("/techologis/p/{pid}/")
async def product_generated_page(request: Request, pid: str):
    """Страница продукта, сгенерированная из БД (C1). Для продуктов с БД-карточкой
    без флага detail_custom (кастомные страницы — italidia, neyrosotrudnik)."""
    from app.products_store import catalog
    from app.billing import get_tariff

    prod = next((p for p in catalog() if p["id"] == pid), None)
    if not prod or prod.get("detail_custom") or prod.get("detail_url"):
        raise HTTPException(status_code=404, detail="Продукт не найден")

    tariff = get_tariff(pid)
    if tariff and not prod.get("price_rub"):
        prod = {**prod, "price_rub": tariff.price_rub or prod.get("price_rub", 0)}

    features = [f.strip() for f in str(prod.get("features") or "").split("|") if f.strip()]
    buyable = [p for p in catalog() if p["status"] == "available" and p["price_rub"] > 0 and p["id"] != "italidia_test"]

    # Тарифные планы продукта (ТЗ tarify v2): карточки на странице продукта
    from app.tariffs_store import list_plans
    plans = list_plans(pid)
    # Подпись к resource_limit (kupec: слежки «Жду скидку», иначе — ресурсы)
    limit_label = "Слежек" if pid == "kupec" else "Лимит ресурсов"

    return templates.TemplateResponse(
        request,
        "product_detail.html",
        {
            "vk_app_id": config.VK_APP_ID or None,
            "is_widget": False,
            "layout_class": "layout-site",
            "page_title": f"{prod['name']} — АЛТ",
            "p": prod,
            "features": features,
            "plans": plans,
            "limit_label": limit_label,
            "category_label": "услуга" if prod.get("category") == "service" else "продукт",
            "buyable": buyable,
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
    customer_id = data.get("customer_id", "").strip()
    from app.products_store import catalog
    from app import promo_store

    prod = next((p for p in catalog() if p["id"] == product_id), None)
    if not prod:
        return JSONResponse({"error": f"Unknown product: {product_id}"}, status_code=404)
    if prod["status"] in ("free", "inquiry", "coming", "dev"):
        return JSONResponse(
            {"error": f"Product '{prod['name']}' is not available for purchase yet. Status: {prod['status']}"},
            status_code=400,
        )

    # Единый источник правды: тариф из БД (админки); клиентская цена игнорируется.
    from app.billing import get_tariff
    from app.tariffs_store import list_plans

    tariff = get_tariff(product_id)
    amount_rub = tariff.price_rub if tariff and tariff.price_rub else prod.get("price_rub", 0)
    period_days = tariff.period_days if tariff and tariff.period_days else 30

    # C4: промокод — скидка считается только на сервере.
    # Тарифные планы (ТЗ Vitrina tarify v2): план перекрывает базовый тариф.
    plan_code = str(data.get("plan") or "").strip().lower()
    plan_name = ""
    plans = list_plans(product_id)
    if plans:
        if plan_code:
            plan_row = next(
                (pl for pl in plans if pl["plan"] == plan_code and pl["active"]), None
            )
            if not plan_row:
                return JSONResponse(
                    {"error": f"Тарифный план '{plan_code}' не найден или неактивен"},
                    status_code=400,
                )
            amount_rub = plan_row["price_rub"]
            plan_name = plan_row["name"]
        else:
            base_row = next((pl for pl in plans if pl["plan"] == "base"), None)
            if base_row:
                amount_rub = base_row["price_rub"]
    promo_code = str(data.get("promo_code") or "").strip()
    promo_use: dict = {}
    if promo_code:
        check = promo_store.validate(promo_code, product_id, amount_rub)
        if not check or check.get("error") or "final_rub" not in check:
            return JSONResponse({"error": (check or {}).get("error") or "Промокод недействителен"}, status_code=400)
        amount_rub = check["final_rub"]
        promo_use = check
    # return_url нужен только боевому режиму ЮKassa; берём адрес текущего запроса,
    # чтобы не зависеть от PUBLIC_BASE_URL (staging/IP/туннель).
    base = str(request.base_url).rstrip("/")
    return_url = data.get("return_url", "") or f"{base}/techologis/?payment=success"
    description = data.get("description", "") or (
        f"Оплата: {prod['name']} — {plan_name}" if plan_name else f"Оплата: {prod['name']}"
    )
    # save_payment_method=true — сохранить карту для автопродления (рекуррент),
    # используется ботами-подписками (ВежПом, НейроСотрудник).
    save_pm = bool(data.get("save_payment_method")) or prod.get("autorenew") is True

    try:
        result = await create_payment(
            product_id=product_id,
            amount_rub=amount_rub,
            description=description,
            return_url=return_url,
            save_payment_method=save_pm,
        )
    except Exception as exc:
        # Нет разрешения на рекуррентные платежи у магазина — создаём одноразовый платёж.
        if save_pm and "recurring" in str(exc).lower():
            save_pm = False
            try:
                result = await create_payment(
                    product_id=product_id,
                    amount_rub=amount_rub,
                    description=description,
                    return_url=return_url,
                    save_payment_method=False,
                )
            except Exception as exc2:
                return JSONResponse({"error": str(exc2)}, status_code=500)
        else:
            return JSONResponse({"error": str(exc)}, status_code=500)

    # Record in local DB
    pm_id = result.get("payment_method", {}).get("id", "")
    record_payment(
        yookassa_id=result["id"],
        product_id=product_id,
        amount_rub=amount_rub,
        status=result.get("status", "pending"),
        customer_id=customer_id,
        payment_method_id=pm_id,
        plan=plan_code,
    )
    if promo_use:
        promo_store.record_use(
            promo_use["code"], product_id, customer_id,
            promo_use["original_rub"], promo_use["final_rub"],
        )

    return JSONResponse({
        "payment_id": result["id"],
        "status": result.get("status", "pending"),
        "confirmation_url": result.get("confirmation", {}).get("confirmation_url", ""),
        "test": result.get("test", False),
    })


@router.post("/api/promo/validate")
async def api_promo_validate(request: Request):
    """Проверить промокод для продукта (без создания платежа)."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    from app import promo_store
    from app.billing import get_tariff

    product_id = str(data.get("product_id") or "").strip()
    code = str(data.get("promo_code") or "")
    tariff = get_tariff(product_id)
    price = tariff.price_rub if tariff and tariff.price_rub else 0
    if not price:
        return JSONResponse({"error": "Продукт недоступен для оплаты"}, status_code=400)
    result = promo_store.validate(code, product_id, price)
    if not result or result.get("error"):
        return JSONResponse(result or {"error": "Промокод недействителен"}, status_code=400)
    return JSONResponse(result)


@router.post("/api/reviews/submit")
async def api_review_submit(request: Request):
    """Отзыв от клиента — на модерацию (status=draft)."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    from app.reviews_store import upsert_review

    text = str(data.get("text") or "").strip()
    if len(text) < 20:
        return JSONResponse({"error": "Расскажите чуть подробнее (минимум 20 знаков)"}, status_code=400)
    if data.get("consent") not in (1, "1", True):
        return JSONResponse({"error": "Требуется согласие на обработку данных отзыва"}, status_code=400)
    review = upsert_review({
        "author": str(data.get("author") or "").strip()[:120] or "Клиент АЛТ",
        "text": text[:2000],
        "rating": data.get("rating") or 5,
        "product_id": str(data.get("product_id") or "").strip(),
        "status": "draft",
        "consent": 1,
    })
    from app.observability import json_log
    json_log({"type": "review_submitted", "review_id": review["id"]})
    return JSONResponse({"ok": True, "message": "Спасибо! Отзыв получен — опубликуем после проверки."})


@router.get("/api/payment-demo")
async def payment_demo_redirect(
    yookassa_id: str = "",
    product_id: str = "",
    customer_id: str = "",
):
    """Sandbox: simulate successful payment redirect."""
    if yookassa_id and yookassa_id.startswith("sandbox-"):
        update_payment_status(yookassa_id, "succeeded")
        # Activate subscription in sandbox
        if customer_id and product_id and product_id in TARIFFS:
            tariff = TARIFFS[product_id]
            get_or_create_balance(
                customer_id=customer_id,
                product_id=product_id,
                allowance_tokens=tariff.token_allowance,
                period_days=tariff.period_days,
            )
    return RedirectResponse(url="/techologis/?payment=success")


@router.post("/api/payment-webhook")
async def payment_webhook(request: Request):
    """ЮKassa webhook: handle payment.succeeded etc.

    Уведомления ЮKassa не подписываются (нет Authorization/HMAC), поэтому
    подлинность проверяем перезапросом статуса платежа в ЮKassa API
    (рекомендованный ЮKassa способ) — активируем только реально успешные.
    """
    try:
        body_bytes = await request.body()
        event = json.loads(body_bytes)
    except Exception:
        return JSONResponse({"error": "Invalid body"}, status_code=400)

    event_type = event.get("event", "")
    payment_obj = event.get("object", {})

    if event_type == "payment.succeeded":
        payment_id = payment_obj.get("id", "")
        # Перепроверяем фактический статус в ЮKassa (sandbox запрос = заглушка)
        try:
            real = await get_payment(payment_id)
        except Exception:
            real = payment_obj  # API недоступен — доверяем событию
        if real.get("status") != "succeeded":
            return JSONResponse({"ok": True})  # событие не подтвердилось — игнорируем

        pm_id = (payment_obj.get("payment_method") or {}).get("id", "") or \
            (real.get("payment_method") or {}).get("id", "")
        update_payment_status(payment_id, "succeeded", payment_method_id=pm_id)
        # Get stored payment record and activate subscription
        rec = get_payment_record(payment_id)
        if rec and rec["customer_id"] and rec["product_id"]:
            # План из платежа (ТЗ tarify v2): allowance/period бёрём из плана, если есть
            plan = (rec.get("plan") or "").strip()
            allowance = None
            period_days = None
            if plan:
                from app.tariffs_store import list_plans
                pl = next((x for x in list_plans(rec["product_id"]) if x["plan"] == plan), None)
                if pl:
                    allowance = pl["resource_limit"] * 100_000 if pl["resource_limit"] else None
                    period_days = pl["period_days"]
            tariff = TARIFFS.get(rec["product_id"])
            if tariff:
                get_or_create_balance(
                    customer_id=rec["customer_id"],
                    product_id=rec["product_id"],
                    allowance_tokens=allowance if allowance else tariff.token_allowance,
                    period_days=period_days or tariff.period_days,
                )
            # План подписки — в subscriptions (ТЗ Купец v2 §7): /api/subscription
            # отдаёт план ботам. Раньше таблица не писалась и гейтинг фолбэчил
            # всех платных на первый план.
            from app.payments import activate_subscription
            try:
                activate_subscription(
                    rec["id"], rec["product_id"], str(rec.get("customer_email") or ""),
                    pm_id,
                    period_days=period_days or (tariff.period_days if tariff else 30),
                    plan=plan,
                    customer_id=rec["customer_id"],
                )
            except Exception:
                pass

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
            period_days=tariff.period_days,
        )

    return JSONResponse({
        "status": "activated",
        "customer_id": customer_id,
        "product_id": product_id,
        "allowance_tokens": allowance,
        "remaining_tokens": get_remaining_tokens(customer_id, product_id),
        "expires_at": bal.get("expires_at", ""),
    })


@router.get("/api/tariffs/{product_id}")
async def api_list_tariff_plans(product_id: str):
    """Активные тарифные планы продукта (для страницы продукта и ботов)."""
    from app.tariffs_store import list_plans

    return JSONResponse(
        {
            "product_id": product_id,
            "plans": [
                {
                    "plan": p["plan"],
                    "name": p["name"],
                    "price_rub": p["price_rub"],
                    "period_days": p["period_days"],
                    "trial_days": p["trial_days"],
                    "resource_limit": p["resource_limit"],
                    "searches_per_month": p.get("searches_per_month") or 0,
                }
                for p in list_plans(product_id)
            ],
        }
    )


@router.get("/api/subscription/{customer_id}/{product_id}")
async def api_get_subscription(customer_id: str, product_id: str):
    """Статус подписки клиента.

    trial=true — идёт зарегистрированный пробный период (ТЗ Купец v2 §7.3):
    боты применяют «триал = все опции старшего плана». searches_per_month —
    месячный пакет поисков плана (для ботов с гейтингом по API).
    """
    bal = get_balance(customer_id, product_id)

    sub_plan = ""
    plan_name = ""
    resource_limit = 0
    searches_per_month = 0
    trial = False
    sub_expires = ""
    try:
        from app.payments import latest_subscription_row
        row = latest_subscription_row(customer_id, product_id)
        if row:
            sub_expires = row.get("expires_at") or ""
            if row.get("plan"):
                sub_plan = row["plan"]
                from app.tariffs_store import list_plans
                pl = next(
                    (x for x in list_plans(product_id) if x["plan"] == sub_plan), None
                )
                if pl:
                    plan_name = pl["name"]
                    resource_limit = pl["resource_limit"]
                    searches_per_month = pl.get("searches_per_month") or 0
            if row.get("status") == "trial":
                try:
                    trial = bool(sub_expires) and datetime.fromisoformat(sub_expires) > datetime.now(timezone.utc)
                except (ValueError, TypeError):
                    trial = False

    except Exception:
        pass

    if bal is None:
        # Токен-баланса нет (триал без платежа) — триал всё равно отдаём
        if trial:
            return JSONResponse({
                "customer_id": customer_id,
                "product_id": product_id,
                "status": "trial",
                "trial": True,
                "plan": sub_plan,
                "plan_name": plan_name or "Пробный период",
                "resource_limit": resource_limit,
                "searches_per_month": searches_per_month,
                "expires_at": sub_expires,
                "allowance_tokens": 0,
                "used_tokens": 0,
                "remaining_tokens": 0,
                "percent_used": 0,
                "balance_low": False,
                "usage_summary": get_usage_summary(customer_id),
            })
        return JSONResponse({"status": "no_subscription"}, status_code=404)

    remaining = get_remaining_tokens(customer_id, product_id)
    low = is_balance_low(customer_id, product_id)
    usage_stats = get_usage_summary(customer_id)

    return JSONResponse({
        "customer_id": customer_id,
        "product_id": product_id,
        "status": bal["status"],
        "trial": trial,
        "allowance_tokens": bal["allowance_tokens"],
        "used_tokens": bal["used_tokens"],
        "remaining_tokens": remaining,
        "percent_used": round(bal["used_tokens"] / bal["allowance_tokens"] * 100, 1) if bal["allowance_tokens"] else 0,
        "balance_low": low,
        "expires_at": bal["expires_at"],
        "usage_summary": usage_stats,
        "plan": sub_plan,
        "plan_name": plan_name,
        "resource_limit": resource_limit,
        "searches_per_month": searches_per_month,
    })


@router.post("/api/subscription/trial/start")
async def api_subscription_trial_start(request: Request):
    """Старт пробного периода (ТЗ Купец v2 §7.3): один на клиента+продукт, навсегда.

    Триал регистрируется записью status='trial' в subscriptions; что делать
    триалу (набор опций старшего плана), решает бот по флагу trial=true.
    """
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    customer_id = str(data.get("customer_id") or "").strip()
    product_id = str(data.get("product_id") or "").strip()
    plan_code = str(data.get("plan") or "").strip().lower()
    if not customer_id or not product_id:
        return JSONResponse({"error": "customer_id и product_id обязательны"}, status_code=400)

    from app.tariffs_store import list_plans, seed_defined_products
    plans = list_plans(product_id)
    if not plans:
        seed_defined_products()
        plans = list_plans(product_id)
    if not plans:
        return JSONResponse({"error": f"У продукта {product_id} нет тарифных планов"}, status_code=404)

    if plan_code:
        pl = next((x for x in plans if x["plan"] == plan_code and x["active"]), None)
        if not pl:
            return JSONResponse({"error": f"Неизвестный план: {plan_code}"}, status_code=400)
    else:
        pl = plans[0]

    from app.payments import start_trial_subscription
    res = start_trial_subscription(
        product_id, customer_id, pl["plan"], pl["trial_days"]
    )
    if not res["started"]:
        row = res.get("row") or {}
        return JSONResponse({
            "trial": False,
            "reason": res["reason"],
            "plan": row.get("plan", ""),
            "expires_at": row.get("expires_at", ""),
        })
    return JSONResponse({
        "trial": True,
        "customer_id": customer_id,
        "product_id": product_id,
        "plan": pl["plan"],
        "plan_name": pl["name"],
        "trial_days": pl["trial_days"],
        "expires_at": (res.get("row") or {}).get("expires_at", ""),
    })


@router.get("/techologis/subscription/")
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


@router.post("/api/subscription/check")
async def api_subscription_check(request: Request):
    """Проверить, можно ли выполнить AI-запрос для клиента по продукту.

    Перед каждым AI-запросом вызывать этот эндпоинт.
    Если лимит исчерпан — вернёт {ok: false, reason: "exhausted", message: "..."}.
    """
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    customer_id = data.get("customer_id", "").strip()
    product_id = data.get("product_id", "").strip()
    if not customer_id or not product_id:
        return JSONResponse({"error": "customer_id and product_id are required"}, status_code=400)

    result = check_before_inference(customer_id, product_id)
    if result["ok"]:
        remaining = get_remaining_tokens(customer_id, product_id)
        bal = get_balance(customer_id, product_id)
        return JSONResponse({
            "ok": True,
            "remaining_tokens": remaining,
            "percent_used": round((bal["used_tokens"] / bal["allowance_tokens"]) * 100, 1) if bal and bal["allowance_tokens"] else 0,
            "balance_low": is_balance_low(customer_id, product_id),
        })
    return JSONResponse(result, status_code=403)


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


# ---------------------------------------------------------------------------
# Автопродление, возвраты, отвязка карты (консолидированная касса)
# ---------------------------------------------------------------------------

@router.post("/api/subscription/renew")
async def api_subscription_renew(request: Request):
    """Автопродление подписки по сохранённой карте (рекуррентный платёж ЮKassa).

    Тело: {customer_id, product_id}
    Берём последний сохранённый payment_method_id клиента по продукту,
    списываем стоимость тарифа без подтверждения, при успехе продлеваем срок.
    """
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    customer_id = str(data.get("customer_id") or "").strip()
    product_id = str(data.get("product_id") or "").strip()
    if not customer_id or not product_id:
        return JSONResponse({"error": "customer_id and product_id are required"}, status_code=400)

    tariff = TARIFFS.get(product_id)
    if not tariff or not tariff.price_rub:
        return JSONResponse({"error": "Unknown or free product"}, status_code=404)

    pm_id = get_payment_method_id(customer_id, product_id)
    if not pm_id:
        return JSONResponse({"error": "Нет сохранённого способа оплаты"}, status_code=404)

    # В sandbox-режиме не ходим в ЮKassa — продлеваем сразу.
    if config.IS_YOOKASSA_SANDBOX:
        bal = renew_balance(customer_id, product_id, tariff.period_days or 30)
        if bal is None:
            return JSONResponse({"error": "Нет активной подписки для продления"}, status_code=404)
        return JSONResponse({"ok": True, "status": "succeeded", "balance": bal})

    try:
        result = await create_recurring_payment(
            payment_method_id=pm_id,
            amount_rub=tariff.price_rub,
            description=f"Автопродление: {tariff.name}",
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

    payment_id = result.get("id", "")
    status = result.get("status", "pending")
    record_payment(
        yookassa_id=payment_id,
        product_id=product_id,
        amount_rub=tariff.price_rub,
        status=status,
        customer_id=customer_id,
        payment_method_id=pm_id,
    )
    if status == "succeeded":
        bal = renew_balance(customer_id, product_id, tariff.period_days or 30)
        return JSONResponse({"ok": True, "status": "succeeded", "payment_id": payment_id, "balance": bal})

    return JSONResponse({
        "ok": False,
        "status": status,
        "payment_id": payment_id,
        "cancel_reason": (result.get("cancellation_details") or {}).get("reason", ""),
    })


@router.post("/api/payment/refund")
async def api_payment_refund(request: Request):
    """Возврат платежа через ЮKassa. Тело: {payment_id, amount_rub?}."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    payment_id = str(data.get("payment_id") or "").strip()
    if not payment_id:
        return JSONResponse({"error": "payment_id is required"}, status_code=400)
    amount = data.get("amount_rub")
    try:
        amount = float(amount) if amount not in (None, "") else None
    except (TypeError, ValueError):
        return JSONResponse({"error": "amount_rub must be a number"}, status_code=400)

    if config.IS_YOOKASSA_SANDBOX:
        update_payment_status(payment_id, "refunded")
        return JSONResponse({"ok": True, "status": "succeeded", "test": True})

    try:
        result = await create_refund(payment_id, amount)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    update_payment_status(payment_id, "refunded")
    return JSONResponse({"ok": True, "status": result.get("status", "pending"), "refund_id": result.get("id", "")})


@router.post("/api/payment-method/delete")
async def api_payment_method_delete(request: Request):
    """Отвязать сохранённую карту клиента по продукту.
    Тело: {customer_id, product_id}."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    customer_id = str(data.get("customer_id") or "").strip()
    product_id = str(data.get("product_id") or "").strip()
    if not customer_id or not product_id:
        return JSONResponse({"error": "customer_id and product_id are required"}, status_code=400)

    pm_id = get_payment_method_id(customer_id, product_id)
    if not pm_id:
        return JSONResponse({"ok": True, "removed": False})  # карты и так нет

    if not config.IS_YOOKASSA_SANDBOX:
        try:
            await delete_payment_method(pm_id)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    from contextlib import closing
    from app.payments import _connect as _pay_conn

    with closing(_pay_conn()) as db:
        db.execute(
            """UPDATE payments SET payment_method_id = ''
               WHERE customer_id = ? AND product_id = ? AND payment_method_id = ?""",
            (customer_id, product_id, pm_id),
        )
        db.commit()
    return JSONResponse({"ok": True, "removed": True})