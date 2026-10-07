"""Billing admin API — управление подписками и учётом.

Все эндпоинты защищены Basic Auth (admin / 20rusalt13).
Центральная точка для:
  - Просмотра всех подписок и расхода
  - Выдачи бесплатного/пробного доступа
  - Отзыва подписки
  - Логирования расхода от продуктов (ItaLidia, ВежПом и др.)
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status as http_status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from app import config
from app.billing import (
    TARIFFS,
    check_before_inference,
    deactivate_expired_subscriptions,
    format_token_allowance,
    get_balance,
    get_expired_subscriptions,
    get_or_create_balance,
    get_remaining_tokens,
    get_usage_summary,
    is_balance_low,
    record_usage,
    top_up_balance,
)

router = APIRouter(tags=["billing-admin"])
templates = Jinja2Templates(directory=str(config.BASE_DIR / "app" / "templates"))

_basic = HTTPBasic(auto_error=False)


def _require_admin_auth(credentials: HTTPBasicCredentials | None = Depends(_basic)) -> str:
    """Проверка Basic Auth для админ-панели."""
    if credentials is None:
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )
    ok_user = secrets.compare_digest(credentials.username or "", "admin")
    ok_pass = secrets.compare_digest(credentials.password or "", "20rusalt13")
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


# ─── Админ-страница биллинга ───────────────────────────────────────────────


@router.get(
    "/admin/billing/",
    response_class=HTMLResponse,
    dependencies=[Depends(_require_admin_auth)],
)
async def admin_billing_page(request: Request) -> HTMLResponse:
    """Главная страница администрирования биллинга."""
    return templates.TemplateResponse(
        request,
        "admin_billing.html",
        {
            "vk_app_id": config.VK_APP_ID or None,
            "is_widget": False,
            "layout_class": "layout-site",
            "page_title": "Биллинг — админ",
        },
    )


# ─── API: получить общую сводку ────────────────────────────────────────────


@router.get(
    "/api/billing/overview",
    dependencies=[Depends(_require_admin_auth)],
)
async def api_billing_overview():
    """Полная сводка по всем подпискам и тарифам."""
    from app.billing import _connect_usage
    from contextlib import closing

    # Все активные подписки
    with closing(_connect_usage()) as db:
        balances = db.execute(
            """SELECT * FROM token_balances ORDER BY id DESC"""
        ).fetchall()
        # Суммарный расход по продуктам
        usage = db.execute(
            """SELECT product_id,
                      SUM(prompt_tokens) as total_prompt,
                      SUM(completion_tokens) as total_completion,
                      SUM(cost_rub) as total_cost,
                      COUNT(*) as total_requests
               FROM token_usage
               GROUP BY product_id"""
        ).fetchall()

    items = []
    for b in balances:
        remaining = b["allowance_tokens"] - b["used_tokens"]
        pct = round(b["used_tokens"] / b["allowance_tokens"] * 100, 1) if b["allowance_tokens"] else 0
        items.append({
            "id": b["id"],
            "customer_id": b["customer_id"],
            "product_id": b["product_id"],
            "subscription_id": b["subscription_id"],
            "allowance_tokens": b["allowance_tokens"],
            "used_tokens": b["used_tokens"],
            "remaining_tokens": max(0, remaining),
            "percent_used": pct,
            "expires_at": b["expires_at"],
            "status": b["status"],
        })

    usage_by_product = {}
    for u in usage:
        usage_by_product[u["product_id"]] = {
            "prompt": u["total_prompt"],
            "completion": u["total_completion"],
            "cost": round(u["total_cost"], 2),
            "requests": u["total_requests"],
        }

    tariffs_info = {}
    for pid, t in TARIFFS.items():
        tariffs_info[pid] = {
            "name": t.name,
            "price": t.price_rub,
            "allowance": format_token_allowance(t.token_allowance),
            "model": t.routerai_model_id,
        }

    active = sum(1 for i in items if i["status"] == "active")
    expired = sum(1 for i in items if i["status"] == "expired")
    total_revenue = sum(
        TARIFFS.get(i["product_id"]).price_rub if TARIFFS.get(i["product_id"]) else 0
        for i in items if i["status"] == "active"
    )

    return JSONResponse({
        "subscriptions": items,
        "usage_by_product": usage_by_product,
        "tariffs": tariffs_info,
        "stats": {
            "active": active,
            "expired": expired,
            "total": len(items),
            "monthly_revenue_est": total_revenue,
        },
    })


# ─── API: выдать доступ (бесплатно / пробный / админский) ─────────────────



    

@router.post(
    "/api/billing/grant-access",
    dependencies=[Depends(_require_admin_auth)],
)
async def api_grant_access(request: Request):
    """Выдать бесплатный/пробный/админский доступ.

    Body (JSON):
      customer_id  — обязателен
      product_id   — обязателен
      tokens       — количество токенов (0 = без лимита / по тарифу)
      period_days  — срок в днях (0 = 30 по умолчанию)
      reason       — причина (бесплатно / учитель / админ / тест)
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    cid = (body.get("customer_id") or "").strip()
    pid = (body.get("product_id") or "").strip()

    if not cid or not pid:
        return JSONResponse(
            {"ok": False, "error": "customer_id и product_id обязательны"},
            status_code=400,
        )

    tariff = TARIFFS.get(pid)
    tokens = body.get("tokens", 0) if body.get("tokens", 0) > 0 else (tariff.token_allowance if tariff else 100_000)
    days = body.get("period_days", 0) if body.get("period_days", 0) > 0 else 30

    bal = get_or_create_balance(
        customer_id=cid,
        product_id=pid,
        allowance_tokens=tokens,
        subscription_id=0,
        period_days=days,
    )

    return JSONResponse({
        "ok": True,
        "balance": bal,
        "granted": {
            "customer_id": cid,
            "product_id": pid,
            "tokens": tokens,
            "period_days": days,
            "reason": body.get("reason", ""),
        },
    })


# ─── API: отозвать подписку ────────────────────────────────────────────────


@router.post(
    "/api/billing/revoke",
    dependencies=[Depends(_require_admin_auth)],
)
async def api_revoke_subscription(request: Request):
    """Деактивировать подписку."""
    from app.billing import _connect_usage
    from contextlib import closing

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)

    balance_id = body.get("balance_id") or 0
    customer_id = body.get("customer_id") or ""
    product_id = body.get("product_id") or ""

    with closing(_connect_usage()) as db:
        if balance_id:
            db.execute(
                "UPDATE token_balances SET status = 'expired' WHERE id = ?",
                (balance_id,),
            )
        elif customer_id and product_id:
            db.execute(
                "UPDATE token_balances SET status = 'expired' WHERE customer_id = ? AND product_id = ?",
                (customer_id, product_id),
            )
        else:
            return JSONResponse(
                {"ok": False, "error": "укажите balance_id или customer_id + product_id"},
                status_code=400,
            )
        db.commit()

    return JSONResponse({"ok": True})


# ─── API: центральный учёт расхода токенов (для продуктов) ─────────────────


@router.post("/api/billing/log-usage")
async def api_log_usage(request: Request):
    """Записать расход токенов от продукта (ItaLidia, ВежПом и др.).

    Body (JSON):
      customer_id       — ID пользователя (telegram_id и т.п.)
      product_id        — ID продукта (italidia, vezhpom…)
      model             — модель RouterAI
      prompt_tokens     — входящие токены
      completion_tokens — исходящие токены
      cost_rub          — опционально, стоимость в рублях
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)

    cid = str(body.get("customer_id", "")).strip()
    pid = str(body.get("product_id", "")).strip()

    if not cid or not pid:
        return JSONResponse(
            {"ok": False, "error": "customer_id и product_id обязательны"},
            status_code=400,
        )

    result = record_usage(
        customer_id=cid,
        product_id=pid,
        model=str(body.get("model", "")),
        prompt_tokens=int(body.get("prompt_tokens", 0)),
        completion_tokens=int(body.get("completion_tokens", 0)),
        cost_rub=float(body.get("cost_rub", 0)),
    )

    return JSONResponse({
        "ok": True,
        "remaining_tokens": max(0, result.get("allowance_tokens", 0) - result.get("used_tokens", 0)),
    })


# ─── API: проверка доступа для продукта ──────────────────────────────────────


@router.post("/api/billing/check-access")
async def api_check_access(request: Request):
    """Проверить, может ли пользователь выполнить AI-запрос.

    Используется продуктами (ItaLidia и др.) вместо дублирования логики.
    Body (JSON):
      customer_id
      product_id
    Returns:
      {"ok": true} или {"ok": false, "reason": "...", "message": "..."}
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)

    cid = str(body.get("customer_id", "")).strip()
    pid = str(body.get("product_id", "")).strip()

    result = check_before_inference(cid, pid)
    return JSONResponse(result)


# ─── API: получить одного клиента ──────────────────────────────────────────


@router.get(
    "/api/billing/customer/{customer_id}",
    dependencies=[Depends(_require_admin_auth)],
)
async def api_customer_detail(customer_id: str):
    """Полная информация о клиенте: все подписки и расход."""
    from app.billing import _connect_usage
    from contextlib import closing

    with closing(_connect_usage()) as db:
        rows = db.execute(
            """SELECT * FROM token_balances
               WHERE customer_id = ?
               ORDER BY id DESC""",
            (customer_id,),
        ).fetchall()
        usage = db.execute(
            """SELECT product_id, SUM(prompt_tokens) as prompt,
                      SUM(completion_tokens) as completion,
                      SUM(cost_rub) as cost,
                      COUNT(*) as requests
               FROM token_usage
               WHERE customer_id = ?
               GROUP BY product_id""",
            (customer_id,),
        ).fetchall()

    return JSONResponse({
        "customer_id": customer_id,
        "subscriptions": [dict(r) for r in rows],
        "usage": [dict(u) for u in usage],
    })