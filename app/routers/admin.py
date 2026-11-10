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

from app import config, site_settings
from app.products_store import (
    ALL_STATUSES,
    catalog,
    delete_product,
    get_db_product,
    upsert_product,
)

from app.templating import templates  # noqa: E402  — общий экземпляр с content_block

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
    _sync_product_tariff(str(body.get("id") or ""), body)
    return JSONResponse({"ok": True, "product": row})


def _sync_product_tariff(pid: str, body: dict) -> None:
    """Форма «Товары» — единая точка: цена/период/триал пишутся и в тарифы,
    иначе тарифы перезатирают правку при следующем чтении (баг 01.11)."""
    fields = {
        k: body.get(k)
        for k in ("price_rub", "period_days", "trial_days")
        if body.get(k) is not None
    }
    if fields:
        from app import tariffs_store

        tariffs_store.upsert_tariffs({pid: fields})


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
    _sync_product_tariff(pid, body)
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
    from app.payments import log_admin_action
    log_admin_action(
        "grant_trial", "subscription", f"{customer_id}/{pid}",
        detail=f"{days} дн., {format_token_allowance(tokens)}",
        admin_ip=request.client.host if request.client else "",
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


# ─── Контент-блоки страниц (Этап A1) ─────────────────────────────────────


@router.get("/admin/api/content-blocks")
async def api_content_blocks(
    page: str = "", _user: str = Depends(_require_admin_auth)
) -> JSONResponse:
    from app import content_blocks

    if page and page not in content_blocks.SCHEMA:
        return JSONResponse({"ok": False, "error": "неизвестная страница"}, status_code=400)
    pids = [pid for pid in content_blocks.SCHEMA if not page or pid == page]
    schema = {
        pid: [
            {"key": key, "label": label, "type": ftype, "default": default}
            for key, label, ftype, default in content_blocks.SCHEMA[pid]
        ]
        for pid in pids
    }
    values = {pid: content_blocks.blocks_for(pid) for pid in pids}
    return JSONResponse(
        {
            "ok": True,
            "pages": [
                {"id": pid, "title": content_blocks.PAGE_TITLES.get(pid, pid)}
                for pid in pids
            ],
            "schema": schema,
            "values": values,
        }
    )


@router.post("/admin/api/content-blocks")
async def api_content_blocks_save(
    request: Request, _user: str = Depends(_require_admin_auth)
):
    from app import content_blocks

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
    page = str(body.get("page") or "")
    blocks = body.get("blocks")
    if page not in content_blocks.SCHEMA or not isinstance(blocks, dict):
        return JSONResponse({"ok": False, "error": "некорректный запрос"}, status_code=400)
    content_blocks.set_blocks(page, blocks)
    # hero-ключи дублируем в site_settings — старый таб «Главная» остаётся согласован
    if page == "index":
        try:
            site_settings.update(blocks)
        except Exception:
            pass
    return JSONResponse({"ok": True, "values": content_blocks.blocks_for(page)})


# ─── Тарифы (Этап A2/A4) ─────────────────────────────────────────────────


@router.get("/admin/api/tariffs")
async def api_tariffs(_user: str = Depends(_require_admin_auth)) -> JSONResponse:
    from app import tariffs_store

    return JSONResponse({"ok": True, "tariffs": tariffs_store.list_tariffs(force=True)})


@router.post("/admin/api/tariffs")
async def api_tariffs_save(
    request: Request, _user: str = Depends(_require_admin_auth)
):
    from app import tariffs_store

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
    values = body.get("tariffs")
    if not isinstance(values, dict):
        return JSONResponse({"ok": False, "error": "некорректный запрос"}, status_code=400)
    saved = tariffs_store.upsert_tariffs(values)
    return JSONResponse({"ok": True, "saved": saved})


# ─── Тарифные планы (ТЗ tarify v2, generalized) ──────────────────────────


@router.get("/admin/api/plans/{pid}")
async def api_plans_list(pid: str, _user: str = Depends(_require_admin_auth)):
    from app.tariffs_store import list_plans

    return JSONResponse({"ok": True, "plans": list_plans(pid, active_only=False)})


@router.put("/admin/api/plans/{pid}/{plan}")
async def api_plan_upsert(
    pid: str, plan: str, request: Request, _user: str = Depends(_require_admin_auth)
):
    from app.tariffs_store import upsert_plan

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
    body = dict(body)
    body["product_id"] = pid
    body["plan"] = plan
    return JSONResponse({"ok": True, "plan": upsert_plan(body)})


@router.delete("/admin/api/plans/{pid}/{plan}")
async def api_plan_delete(
    pid: str, plan: str, _user: str = Depends(_require_admin_auth)
):
    from app.tariffs_store import delete_plan

    return JSONResponse({"ok": delete_plan(pid, plan)})


# ─── Отзывы/кейсы (Этап C2) ──────────────────────────────────────────────


# ─── Промокоды (Этап C4) ─────────────────────────────────────────────────


@router.get("/admin/api/promos")
async def api_promos(_user: str = Depends(_require_admin_auth)) -> JSONResponse:
    from app.promo_store import list_promos

    return JSONResponse({"ok": True, "promos": list_promos()})


@router.post("/admin/api/promos")
async def api_promo_save(
    request: Request, _user: str = Depends(_require_admin_auth)
):
    from app.promo_store import upsert_promo

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
    try:
        return JSONResponse({"ok": True, "promo": upsert_promo(body)})
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@router.delete("/admin/api/promos/{pid}")
async def api_promo_delete(pid: int, _user: str = Depends(_require_admin_auth)):
    from app.promo_store import delete_promo

    return JSONResponse({"ok": delete_promo(pid)})


@router.get("/admin/api/reviews")
async def api_reviews(_user: str = Depends(_require_admin_auth)) -> JSONResponse:
    from app.reviews_store import list_reviews

    return JSONResponse({"ok": True, "reviews": list_reviews(include_hidden=True)})


@router.post("/admin/api/reviews")
async def api_review_save(
    request: Request, _user: str = Depends(_require_admin_auth)
):
    from app.reviews_store import upsert_review

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
    return JSONResponse({"ok": True, "review": upsert_review(body)})


@router.delete("/admin/api/reviews/{rid}")
async def api_review_delete(rid: int, _user: str = Depends(_require_admin_auth)):
    from app.reviews_store import delete_review

    return JSONResponse({"ok": delete_review(rid)})


@router.post("/admin/api/reviews/upload")
async def api_review_upload(
    request: Request, _user: str = Depends(_require_admin_auth)
):
    """Загрузка картинки отзыва (multipart, ≤5 МБ, png/jpg/webp/gif/svg)."""
    from app.reviews_store import save_photo

    try:
        form = await request.form()
        upload = form.get("file")
        if upload is None or not getattr(upload, "filename", ""):
            return JSONResponse({"ok": False, "error": "нет файла"}, status_code=400)
        content = await upload.read()
        url = save_photo(upload.filename, content)
        return JSONResponse({"ok": True, "url": url})
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ─── Диалоги эксперта (решение Шефа 22.10) ───────────────────────────────


@router.get("/admin/api/expert-dialogs")
async def api_expert_dialogs(
    search: str = "", leads: str = "", _user: str = Depends(_require_admin_auth)
) -> JSONResponse:
    from app import expert_dialogs_store as eds

    sessions = eds.list_sessions(
        search=search.strip(), leads_only=leads in ("1", "true", "yes")
    )
    return JSONResponse({"ok": True, "sessions": sessions, "stats": eds.stats()})


@router.get("/admin/api/expert-dialogs/{session_key}")
async def api_expert_dialog_thread(
    session_key: str, _user: str = Depends(_require_admin_auth)
) -> JSONResponse:
    from app import expert_dialogs_store as eds

    return JSONResponse(
        {"ok": True, "messages": eds.get_session_messages(session_key)}
    )


# ─── Диагностики: заглушка (раздел 5.4 ТЗ) ────────────────────────────────


@router.get("/admin/api/subject-requests")
async def api_subject_requests(_user: str = Depends(_require_admin_auth)) -> JSONResponse:
    from app.diagnostics_store import list_subject_requests

    return JSONResponse({"ok": True, "requests": list_subject_requests()})


@router.post("/admin/api/purge-subject")
async def api_purge_subject(
    request: Request, _user: str = Depends(_require_admin_auth)
):
    """Удаление/обезличивание данных субъекта по запросу (152-ФЗ, ст. 20)."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
    from app.diagnostics_store import log_subject_request, purge_subject

    key = str(body.get("contact") or "").strip()
    if len(key) < 5:
        return JSONResponse({"ok": False, "error": "укажите email или телефон"}, status_code=400)
    removed = purge_subject(key)
    return JSONResponse({"ok": True, "removed": removed})


@router.get("/admin/api/diagnostics")
async def api_diagnostics(_user: str = Depends(_require_admin_auth)) -> JSONResponse:
    """Список заявок диагностик: скоринг/триггеры, PDF, контакты."""
    from app.diagnostics_store import list_diagnostics

    items = list_diagnostics()
    return JSONResponse({"ok": True, "items": items})


@router.get("/admin/api/consistency")
async def api_consistency_run(_user: str = Depends(_require_admin_auth)) -> JSONResponse:
    """E2 ТЗ Развития: чек согласованности (страницы/цены/планы) по требованию.

    Ночной прогон — в main._consistency_loop (04:30 МСК, алерт в MAX);
    здесь тот же чек запускается вручную из админки.
    """
    from app.consistency_check import run_consistency_check

    report = await run_consistency_check()
    return JSONResponse({"ok": True, "report": report})


@router.get("/admin/api/funnel")
async def api_funnel(_user: str = Depends(_require_admin_auth)) -> JSONResponse:
    """G3 ТЗ Развития: воронка за 30 дней и всего — анкеты → лиды → оплаты.

    Метрика-API (G1) не требует: считаем по своим БД (diagnostics/payments/billing).
    """
    from datetime import datetime, timedelta, timezone

    from app.diagnostics_store import list_diagnostics
    from app.payments import _connect as _payments_connect

    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=30)).isoformat()

    diags = list_diagnostics(limit=1000)
    d30 = [d for d in diags if (d.get("created_at") or "") >= since]
    leads30 = [d for d in d30 if d.get("email") or d.get("phone")]

    with closing(_payments_connect()) as db:
        paid30 = db.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(amount_rub), 0) AS sum FROM payments"
            " WHERE status = 'succeeded' AND created_at >= ?",
            (since,),
        ).fetchone()
        paid_all = db.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(amount_rub), 0) AS sum FROM payments"
            " WHERE status = 'succeeded'",
        ).fetchone()
        by_product = db.execute(
            "SELECT product_id, COUNT(*) AS n, COALESCE(SUM(amount_rub), 0) AS sum"
            " FROM payments WHERE status = 'succeeded' AND created_at >= ?"
            " GROUP BY product_id ORDER BY sum DESC LIMIT 6",
            (since,),
        ).fetchall()
        active_subs = db.execute(
            "SELECT COUNT(*) AS n FROM subscriptions WHERE status = 'active'"
            " AND expires_at > ?",
            (now.isoformat(),),
        ).fetchone()

    d30n, l30n = len(d30), len(leads30)
    return JSONResponse({
        "ok": True,
        "diagnostics_30d": d30n,
        "leads_30d": l30n,
        "lead_rate_30d": round(l30n / d30n * 100, 1) if d30n else 0.0,
        "payments_30d": paid30["n"],
        "revenue_30d": paid30["sum"],
        "conversion_30d": round(paid30["n"] / l30n * 100, 1) if l30n else 0.0,
        "payments_total": paid_all["n"],
        "revenue_total": paid_all["sum"],
        "active_subscriptions": active_subs["n"],
        "by_product_30d": [
            {"product_id": r["product_id"], "n": r["n"], "sum": r["sum"]}
            for r in by_product
        ],
        "generated_at": now.replace(microsecond=0).isoformat(),
    })


@router.get("/admin/api/finance/overview")
async def api_finance_overview(_user: str = Depends(_require_admin_auth)) -> JSONResponse:
    """Финансовый дашборд (ТЗ Алика 10.11 §2): доходы, подписки, план,
    возвраты, таблица подписчиков с картами и audit log."""
    from datetime import datetime, timedelta, timezone

    from app.payments import _connect as _pay_conn, list_audit_log
    from app.products_store import catalog

    now = datetime.now(timezone.utc)
    m30 = (now - timedelta(days=30)).isoformat()
    m60 = (now - timedelta(days=60)).isoformat()
    today = now.date().isoformat()

    names = {p["id"]: p["name"] for p in catalog(include_hidden=True)}

    db = _pay_conn()
    try:
        rev30 = db.execute(
            "SELECT COALESCE(SUM(amount_rub),0) AS s, COUNT(*) AS n, COALESCE(AVG(amount_rub),0) AS a"
            " FROM payments WHERE status='succeeded' AND created_at >= ?", (m30,)
        ).fetchone()
        rev_prev = db.execute(
            "SELECT COALESCE(SUM(amount_rub),0) AS s FROM payments"
            " WHERE status='succeeded' AND created_at >= ? AND created_at < ?", (m60, m30)
        ).fetchone()
        refunds30 = db.execute(
            "SELECT COALESCE(SUM(amount_rub),0) AS s FROM payments"
            " WHERE status='refunded' AND updated_at >= ?", (m30,)
        ).fetchone()
        # Подписки: последняя запись на пару клиент+продукт
        subs = db.execute(
            """SELECT customer_id, product_id, status, plan, expires_at, started_at
               FROM subscriptions s
               WHERE id = (SELECT MAX(id) FROM subscriptions s2
                           WHERE s2.customer_id = s.customer_id
                             AND s2.product_id = s.product_id)
               ORDER BY expires_at DESC LIMIT 300""",
        ).fetchall()

        def _last_payment(cid, pid):
            return db.execute(
                """SELECT yookassa_id, amount_rub, pm_brand, pm_last4, payment_method_id, created_at
                   FROM payments WHERE customer_id = ? AND product_id = ? AND status = 'succeeded'
                   ORDER BY id DESC LIMIT 1""",
                (cid, pid),
            ).fetchone()

        rows = []
        by_product = {}
        active = expiring_today = expired = 0
        for s in subs:
            pid = s["product_id"]
            st = s["status"]
            exp = s["expires_at"] or ""
            is_active = st in ("active", "trial") and (not exp or exp > now.isoformat())
            if is_active:
                active += 1
                if exp[:10] == today:
                    expiring_today += 1
            elif exp and exp <= now.isoformat():
                expired += 1
            lp = _last_payment(s["customer_id"], pid)
            amount = lp["amount_rub"] if lp else 0
            if is_active:
                bp = by_product.setdefault(pid, {"active": 0, "revenue_month": 0, "refunds": 0})
                bp["active"] += 1
                bp["revenue_month"] += amount or 0
            rows.append({
                "customer_id": s["customer_id"],
                "product_id": pid,
                "product_name": names.get(pid, pid),
                "status": st,
                "plan": s["plan"] or "",
                "amount_rub": amount,
                "card_label": (f"{(lp['pm_brand'] or '').capitalize()} ••{lp['pm_last4']}" if lp and lp["pm_last4"] else ""),
                "payment_id": lp["yookassa_id"] if lp else "",
                "expires_at": exp[:10],
                "created_at": (s["started_at"] or "")[:10],
                "auto_renew": bool(lp and lp["payment_method_id"]),
            })

        refunds_by_product = {}
        for r in db.execute(
            "SELECT product_id, COALESCE(SUM(amount_rub),0) AS s FROM payments"
            " WHERE status='refunded' AND updated_at >= ? GROUP BY product_id", (m30,)
        ).fetchall():
            refunds_by_product[r["product_id"]] = r["s"]

        by_product_list = []
        for pid, bp in sorted(by_product.items(), key=lambda kv: -kv[1]["revenue_month"]):
            by_product_list.append({
                "product_id": pid,
                "product_name": names.get(pid, pid),
                **bp,
                "refunds": refunds_by_product.get(pid, 0),
            })
    finally:
        db.close()

    plan = int(getattr(config, "MONTH_REVENUE_PLAN_RUB", 0) or 15000)
    revenue = rev30["s"] or 0
    return JSONResponse({
        "ok": True,
        "stats": {
            "total_revenue_month": revenue,
            "revenue_prev_month": rev_prev["s"] or 0,
            "active_subscriptions": active,
            "expiring_today": expiring_today,
            "expired": expired,
            "total_refunds_month": refunds30["s"] or 0,
            "avg_cheque": round(rev30["a"] or 0),
            "payments_count_month": rev30["n"],
            "month_plan": plan,
            "month_plan_pct": round(revenue / plan * 100) if plan else 0,
        },
        "by_product": by_product_list,
        "subscriptions": rows,
        "audit_log": list_audit_log(50),
    })


@router.post("/admin/api/finance/mark-refund")
async def api_finance_mark_refund(request: Request, _user: str = Depends(_require_admin_auth)) -> JSONResponse:
    """Отметить возврат вручную (деньги возвращаются в ЛК ЮKassa —
    здесь только фиксация статуса и запись в audit log, §0/§3.3 ТЗ)."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
    payment_id = str(body.get("payment_id") or "").strip()
    note = str(body.get("note") or "").strip()[:200]
    if not payment_id:
        return JSONResponse({"ok": False, "error": "payment_id обязателен"}, status_code=400)

    from app.payments import _connect as _pay_conn, log_admin_action

    with closing(_pay_conn()) as db:
        row = db.execute(
            "SELECT id, yookassa_id, amount_rub, product_id, customer_id FROM payments"
            " WHERE yookassa_id = ? OR id = ?",
            (payment_id, payment_id if str(payment_id).isdigit() else -1),
        ).fetchone()
        if not row:
            return JSONResponse({"ok": False, "error": "платёж не найден"}, status_code=404)
        amount = body.get("amount")
        amount = int(amount) if amount else row["amount_rub"]
        db.execute(
            "UPDATE payments SET status='refunded', updated_at=datetime('now') WHERE id = ?",
            (row["id"],),
        )
        db.commit()

    log_admin_action(
        "refund_marked", "payment", row["yookassa_id"],
        detail=f"{amount} ₽, {note or 'возврат через ЛК ЮKassa'}",
        admin_ip=request.client.host if request.client else "",
    )
    return JSONResponse({"ok": True, "payment_id": row["yookassa_id"], "amount": amount})
