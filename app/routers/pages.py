from __future__ import annotations

import secrets

from fastapi import APIRouter, Request
from fastapi import Depends, HTTPException, status
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app import config

router = APIRouter()
templates = Jinja2Templates(directory=str(config.BASE_DIR / "app" / "templates"))

_basic = HTTPBasic()


def _require_admin_auth(credentials: HTTPBasicCredentials = Depends(_basic)) -> str:
    ok_user = secrets.compare_digest(credentials.username or "", "admin")
    ok_pass = secrets.compare_digest(credentials.password or "", "20rusalt13")
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    # Starlette 1.x: TemplateResponse(request, name, context) — request первым.
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "vk_app_id": config.VK_APP_ID or None,
            "is_widget": False,
            "layout_class": "layout-site",
            "page_title": "АЛЬТЕРНАТИВА (АЛТ) — альтернативные легальные технологии",
        },
    )


def _site_page(
    request: Request,
    *,
    h1: str,
    description: str,
    hint: str | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "site_page.html",
        {
            "vk_app_id": config.VK_APP_ID or None,
            "is_widget": False,
            "layout_class": "layout-site",
            "page_title": h1,
            "page_h1": h1,
            "page_description": description,
            "page_hint": hint,
        },
    )


@router.get("/laws/", response_class=HTMLResponse)
async def laws(request: Request) -> HTMLResponse:
    return _site_page(
        request,
        h1="Правовая база",
        description="Публичная страница: правовые материалы и ссылки на законы.",
    )


@router.get("/news/", response_class=HTMLResponse)
async def news(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "news.html",
        {
            "vk_app_id": config.VK_APP_ID or None,
            "is_widget": False,
            "layout_class": "layout-site",
            "page_title": "Новости",
        },
    )


@router.get("/techologis/", response_class=HTMLResponse)
async def techologis(request: Request, payment: str = "") -> HTMLResponse:
    """Каталог инструментов АЛТ с токен-биллингом (витрина v2)."""
    from app.billing import TARIFFS, Tariff, get_subscription_text, format_token_allowance
    from app.payments import PRODUCTS

    products_list = [
        p for p in PRODUCTS.values() if p["category"] == "product" and p["status"] != "dev"
    ]
    services_list = [
        p for p in PRODUCTS.values() if p["category"] == "service" or p.get("status") == "dev"
    ]

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


@router.get("/diagnostics/", response_class=HTMLResponse)
async def diagnostics(request: Request) -> HTMLResponse:
    return _site_page(
        request,
        h1="Диагностика",
        description="Публичная страница: описание услуги диагностики.",
    )


@router.get("/channels/", response_class=HTMLResponse)
async def channels(request: Request) -> HTMLResponse:
    return _site_page(
        request,
        h1="Каналы",
        description="Публичная страница: взаимодействие с мессенджерами и соцсетями.",
    )


@router.get("/admin/", response_class=HTMLResponse, dependencies=[Depends(_require_admin_auth)])
async def admin(request: Request) -> HTMLResponse:
    return _site_page(
        request,
        h1="Админ",
        description="Страница с авторизацией: управление сайтом.",
        hint="Доступ ограничен. Здесь появятся инструменты управления сайтом.",
    )


@router.get("/publapprov/", response_class=HTMLResponse, dependencies=[Depends(_require_admin_auth)])
async def publapprov(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "publapprov.html",
        {
            "vk_app_id": config.VK_APP_ID or None,
            "is_widget": False,
            "layout_class": "layout-site",
            "page_title": "Публикации — согласование",
        },
    )


@router.get("/consultant", response_class=HTMLResponse)
async def consultant(request: Request) -> HTMLResponse:
    """Полноэкранный интерфейс АЛТ‑эксперт: чат и шаблоны."""
    return templates.TemplateResponse(
        request,
        "consultant.html",
        {
            "vk_app_id": config.VK_APP_ID or None,
            "is_widget": False,
            "layout_class": "layout-app",
            "page_title": "АЛТ‑эксперт",
        },
    )


@router.get("/widget", response_class=HTMLResponse)
async def widget(request: Request) -> HTMLResponse:
    """Версия для встраивания в сообщество VK (iframe / мини-приложение)."""
    return templates.TemplateResponse(
        request,
        "widget.html",
        {
            "vk_app_id": config.VK_APP_ID or None,
            "is_widget": True,
            "layout_class": "layout-widget",
            "page_title": "Консультант",
        },
    )


@router.get("/talk", response_class=HTMLResponse)
async def talk_page(request: Request) -> HTMLResponse:
    """
    Hidden integration page (no menu links). Pure HTML shell is stored in /talk/public.
    Auth is handled client-side via TALK_KEY, API is protected server-side.
    """
    path = (config.BASE_DIR / "talk" / "public" / "index.html").resolve()
    return FileResponse(path, media_type="text/html; charset=utf-8")
