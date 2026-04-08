from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app import config

router = APIRouter()
templates = Jinja2Templates(directory=str(config.BASE_DIR / "app" / "templates"))


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

@router.get("/consultant", response_class=HTMLResponse)
async def consultant(request: Request) -> HTMLResponse:
    """Прототип интерфейса: чат + шаблоны документов."""
    return templates.TemplateResponse(
        request,
        "consultant.html",
        {
            "vk_app_id": config.VK_APP_ID or None,
            "is_widget": False,
            "layout_class": "layout-app",
            "page_title": "Консультант — прототип",
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
