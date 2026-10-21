"""Единая админ-панель /admin/ — табы: Главная, Товары, Заказы, Биллинг,
Публикации, Диагностики.

REST: /admin/api/*. Авторизация — Basic Auth (admin / ADMIN_PASSWORD из .env).
"""

from __future__ import annotations

import secrets
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security.http import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from app import config, site_settings
from app.products_store import (
    ALL_STATUSES,
    catalog,
    delete_product,
    get_db_product,
    upsert_product,
)

templates = Jinja2Templates(directory=str(config.BASE_DIR / "app" / "templates"))

router = APIRouter(tags=["admin"])
_basic = HTTPBasic(auto_error=False)


def _require_admin_auth(
    credentials: HTTPBasicCredentials | None = Depends(_basic),
) -> str:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": "Basic"},
        )
    ok_user = secrets.compare_digest(credentials.username or "", "admin")
    ok_pass = secrets.compare_digest(credentials.password or "", config.ADMIN_PASSWORD)
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": "Basic"},
        )
    return "admin"


# ─── Страница панели ───────────────────────────────────────────────────────


@router.get("/admin/", response_class=HTMLResponse)
async def admin_panel(
    request: Request, _user: str = Depends(_require_admin_auth)
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "admin_panel.html",
        {
            "vk_app_id": config.VK_APP_ID or None,
            "is_widget": False,
            "layout_class": "layout-site",
            "page_title": "Админ-панель — АЛТ",
            "home_settings": site_settings.get_all(),
        },
    )


# ─── Товары: REST ──────────────────────────────────────────────────────────


@router.get("/admin/api/products")
async def api_products(_user: str = Depends(_require_admin_auth)) -> JSONResponse:
    return JSONResponse({"ok": True, "products": catalog(include_hidden=True)})


@router.post("/admin/api/products")
async def api_product_create(request: Request, _user: str = Depends(_require_admin_auth)):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
    try:
        row = upsert_product(body)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return JSONResponse({"ok": True, "product": row})


@router.put("/admin/api/products/{pid}")
async def api_product_update(
    pid: str, request: Request, _user: str = Depends(_require_admin_auth)
):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
    body = dict(body)
    body["id"] = pid
    try:
        row = upsert_product(body)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return JSONResponse({"ok": True, "product": row})


@router.delete("/admin/api/products/{pid}")
async def api_product_delete(pid: str, _user: str = Depends(_require_admin_auth)):
    existing = get_db_product(pid)
    if not existing:
        return JSONResponse(
            {"ok": False, "error": "товар существует только в статическом каталоге и не может быть удалён"},
            status_code=400,
        )
    delete_product(pid)
    return JSONResponse({"ok": True})


# ─── Заказы и подписки: REST ───────────────────────────────────────────────


@router.get("/admin/api/orders")
async def api_orders(
    request: Request,
    date_from: str = "",
    date_to: str = "",
    product: str = "",
    pay_status: str = "",
    _user: str = Depends(_require_admin_auth),
) -> JSONResponse:
    """Таблица платежей (заказы). Фильтры: дата, продукт, статус оплаты."""
    from app.payments import _connect

    query = "SELECT * FROM payments WHERE 1=1"
    params: list[Any] = []
    if date_from:
        query += " AND date(created_at) >= date(?)"
        params.append(date_from)
    if date_to:
        query += " AND date(created_at) <= date(?)"
        params.append(date_to)
    if product:
        query += " AND product_id = ?"
        params.append(product)
    if pay_status:
        query += " AND status = ?"
        params.append(pay_status)
    query += " ORDER BY created_at DESC LIMIT 500"

    with closing(_connect()) as db:
        rows = db.execute(query, params).fetchall()
        # подписки по customer_id (для статуса подписки в строке заказа)
        subs = db.execute(
            "SELECT * FROM subscriptions ORDER BY started_at DESC"
        ).fetchall()

    sub_by_customer: dict[str, sqlite3.Row] = {}
    for s in subs:
        key = (s["customer_id"] or s["customer_email"] or "").strip().lower()
        if key and key not in sub_by_customer:
            sub_by_customer[key] = s

    items = []
    for r in rows:
        key = (r["customer_id"] or r["customer_email"] or "").strip().lower()
        sub = sub_by_customer.get(key)
        items.append(
            {
                "id": r["id"],
                "yookassa_id": r["yookassa_id"],
                "product_id": r["product_id"],
                "amount_rub": r["amount_rub"],
                "status": r["status"],
                "customer_email": r["customer_email"] or "",
                "customer_id": r["customer_id"] or "",
                "created_at": r["created_at"],
                "subscription_status": sub["status"] if sub else "",
                "subscription_expires": sub["expires_at"] if sub else "",
            }
        )

    from app.payments import PRODUCTS

    product_options = sorted({p["id"] for p in catalog(include_hidden=True)} | set(PRODUCTS))
    return JSONResponse(
        {
            "ok": True,
            "orders": items,
            "product_options": product_options,
            "status_options": ["pending", "succeeded", "canceled", "waiting_for_capture"],
        }
    )


@router.post("/admin/api/orders/{order_id}/activate-trial")
async def api_activate_trial(
    order_id: int, request: Request, _user: str = Depends(_require_admin_auth)
):
    """Ручная активация триала по заказу: выдаёт пробный баланс токенов."""
    from app.billing import TARIFFS, format_token_allowance
    from app.billing import get_or_create_balance
    from app.payments import _connect

    with closing(_connect()) as db:
        row = db.execute("SELECT * FROM payments WHERE id = ?", (order_id,)).fetchone()
    if not row:
        return JSONResponse({"ok": False, "error": "заказ не найден"}, status_code=404)

    try:
        body = await request.json()
    except Exception:
        body = {}
    customer_id = str(
        body.get("customer_id") or row["customer_id"] or row["customer_email"] or ""
    ).strip()
    if not customer_id:
        return JSONResponse(
            {"ok": False, "error": "у заказа нет customer_id/email — укажите вручную"},
            status_code=400,
        )

    pid = row["product_id"]
    tariff = TARIFFS.get(pid)
    tokens = int(body.get("tokens") or 0) or (
        tariff.trial_tokens if tariff and tariff.trial_tokens else 100_000
    )
    days = int(body.get("days") or 0) or (
        tariff.trial_days if tariff and tariff.trial_days else 14
    )

    bal = get_or_create_balance(
        customer_id=customer_id,
        product_id=pid,
        allowance_tokens=tokens,
        subscription_id=0,
        period_days=days,
    )
    return JSONResponse(
        {
            "ok": True,
            "balance": bal,
            "granted": {
                "customer_id": customer_id,
                "product_id": pid,
                "tokens": tokens,
                "tokens_text": format_token_allowance(tokens),
                "period_days": days,
            },
        }
    )


# ─── Биллинг: агрегаты для таба (история расхода) ─────────────────────────


@router.get("/admin/api/billing/history")
async def api_billing_history(_user: str = Depends(_require_admin_auth)) -> JSONResponse:
    """История расхода токенов по дням (последние 30 дней)."""
    from app.billing import _connect_usage

    with closing(_connect_usage()) as db:
        try:
            rows = db.execute(
                """SELECT date(recorded_at) AS day,
                          SUM(prompt_tokens + completion_tokens) AS tokens,
                          SUM(cost_rub) AS cost
                   FROM token_usage
                   WHERE recorded_at >= datetime('now', '-30 days')
                   GROUP BY date(recorded_at)
                   ORDER BY day"""
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
    return JSONResponse(
        {
            "ok": True,
            "history": [
                {"day": r["day"], "tokens": r["tokens"] or 0, "cost": round(r["cost"] or 0, 2)}
                for r in rows
            ],
        }
    )


# ─── Публикации: очистка очереди + тумблер автопубликации ─────────────────


@router.post("/admin/api/content/clear-queue")
async def api_content_clear_queue(_user: str = Depends(_require_admin_auth)) -> JSONResponse:
    """Сбросить очередь: все pending/needs_edit → canceled."""
    from app.content_store import list_items, set_status

    rid = uuid.uuid4().hex
    pending = list_items(statuses={"pending", "needs_edit"})
    done = 0
    for item in pending:
        try:
            set_status(item.publication_id, status="canceled")
            done += 1
        except Exception:
            pass
    return JSONResponse({"ok": True, "cleared": done, "request_id": rid})


@router.get("/admin/api/content/settings")
async def api_content_settings(_user: str = Depends(_require_admin_auth)) -> JSONResponse:
    return JSONResponse({"ok": True, "auto_publish": site_settings.get_auto_publish()})


@router.post("/admin/api/content/settings")
async def api_content_settings_update(
    request: Request, _user: str = Depends(_require_admin_auth)
):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
    value = site_settings.set_auto_publish(bool(body.get("auto_publish")))
    return JSONResponse({"ok": True, "auto_publish": value})


# ─── Главная: редакция hero ───────────────────────────────────────────────


@router.get("/admin/api/home-settings")
async def api_home_settings(_user: str = Depends(_require_admin_auth)) -> JSONResponse:
    return JSONResponse({"ok": True, "settings": site_settings.get_all()})


@router.post("/admin/api/home-settings")
async def api_home_settings_update(
    request: Request, _user: str = Depends(_require_admin_auth)
):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
    settings = site_settings.update(body)
    return JSONResponse({"ok": True, "settings": settings})


# ─── Диагностики: заглушка (раздел 5.4 ТЗ) ────────────────────────────────


@router.get("/admin/api/diagnostics")
async def api_diagnostics(_user: str = Depends(_require_admin_auth)) -> JSONResponse:
    """Список заявок диагностик: скоринг/триггеры, PDF, контакты."""
    from app.diagnostics_store import list_diagnostics

    items = list_diagnostics()
    return JSONResponse({"ok": True, "items": items})
