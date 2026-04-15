from __future__ import annotations

import secrets

from fastapi import APIRouter, Request
from fastapi import Depends, HTTPException, status
from fastapi.responses import HTMLResponse
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
async def techologis(request: Request) -> HTMLResponse:
    return _site_page(
        request,
        h1="Услуги",
        description="Публичная страница: услуги проекта.",
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
