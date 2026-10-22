from __future__ import annotations

import secrets

from fastapi import APIRouter, Request
from fastapi import Depends, HTTPException, status
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app import config

router = APIRouter()
from app.templating import templates  # noqa: E402  — общий экземпляр с content_block

_basic = HTTPBasic()


def _require_admin_auth(credentials: HTTPBasicCredentials = Depends(_basic)) -> str:
    ok_user = secrets.compare_digest(credentials.username or "", "admin")
    ok_pass = secrets.compare_digest(credentials.password or "", config.ADMIN_PASSWORD)
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    from app import site_settings

    settings = site_settings.get_all()
    # Starlette 1.x: TemplateResponse(request, name, context) — request первым.
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "vk_app_id": config.VK_APP_ID or None,
            "is_widget": False,
            "layout_class": "layout-site",
            "page_title": "АЛЬТЕРНАТИВА (АЛТ) — альтернативные легальные технологии",
            "hero_title": settings["hero_title"],
            "hero_subtitle": settings["hero_subtitle"],
            "hero_cta_text": settings["hero_cta_text"],
            "hero_cta_url": settings["hero_cta_url"],
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
    from app.products_store import catalog

    all_catalog = catalog()
    product_order = ["italidia", "vezhpom"]
    products_list = sorted(
        [p for p in all_catalog if p["category"] == "product" and p.get("status") != "dev"],
        key=lambda p: product_order.index(p["id"]) if p["id"] in product_order else len(product_order),
    )
    services_list = [
        p for p in all_catalog if p["category"] == "service" or p.get("status") == "dev"
    ]
    # Все оплачиваемые товары и услуги (для JS-модалки на витрине)
    all_buyable = [
        p for p in all_catalog if p["status"] == "available" and p["price_rub"] > 0
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

    # Плитки-иконки карточек витрины (эмодзи вместо бинарных картинок)
    static_icons = {
        "vezhpom": "📡",
        "italidia": "🇮🇹",
        "neyrosotrudnik": "🤖",
        "alt_expert": "⚖️",
        "kupec": "🛒",
        "anketa_diagnostics": "📋",
        "openclaw": "🦉",
        "ozhivlenie": "🎨",
        "site": "🌐",
        "techologis": "⚙️",
    }
    icons = {
        p["id"]: p.get("icon") or static_icons.get(p["id"], "🧩") for p in all_catalog
    }

    from app.reviews_store import list_reviews
    reviews = list_reviews()

    return templates.TemplateResponse(
        request,
        "techologis_v2.html",
        {
            "vk_app_id": config.VK_APP_ID or None,
            "is_widget": False,
            "layout_class": "layout-site",
            "page_title": "Витрина — АЛЬТЕРНАТИВА (АЛТ)",
            "products": products_list,
            "services": services_list,
            "payment_status": payment,
            "is_sandbox": config.IS_YOOKASSA_SANDBOX,
            "tariffs_info": tariffs_info,
            "billing_enabled": True,
            "all_buyable": all_buyable,
            "icons": icons,
            "reviews": reviews,
        },
    )


@router.get("/diagnostics/", response_class=HTMLResponse)
async def diagnostics(request: Request, payment: str = "") -> HTMLResponse:
    """Страница диагностики с тремя вариантами (бесплатно, ваше дело, платный анализ)."""
    from app.payments import PRODUCTS

    all_buyable = [
        p for p in PRODUCTS.values()
        if p["status"] == "available" and p["price_rub"] > 0
        and p["id"] != "italidia_test"
    ]

    return templates.TemplateResponse(
        request,
        "diagnostics.html",
        {
            "vk_app_id": config.VK_APP_ID or None,
            "is_widget": False,
            "layout_class": "layout-site",
            "page_title": "Диагностика — АЛЬТЕРНАТИВА (АЛТ)",
            "all_buyable": all_buyable,
            "payment_status": payment,
        },
    )


def _diagnostics_form(
    request: Request,
    *,
    form_kind: str,
    form_title: str,
    form_meta: str,
    submit_label: str,
    api_endpoint: str,
    payment: str = "",
) -> HTMLResponse:
    """Общий рендер трёх форм анкет диагностики (ТЗ 5.4.2)."""
    return templates.TemplateResponse(
        request,
        "diagnostics_form.html",
        {
            "vk_app_id": config.VK_APP_ID or None,
            "is_widget": False,
            "layout_class": "layout-site",
            "page_title": f"{form_title} — АЛЬТЕРНАТИВА (АЛТ)",
            "form_kind": form_kind,
            "form_title": form_title,
            "form_meta": form_meta,
            "submit_label": submit_label,
            "api_endpoint": api_endpoint,
            "is_paid": form_kind == "paid",
            "payment_status": payment,
        },
    )


@router.get("/diagnostics/free/", response_class=HTMLResponse)
async def diagnostics_free(request: Request) -> HTMLResponse:
    """Бесплатная диагностика — форма анкеты."""
    return _diagnostics_form(
        request,
        form_kind="free",
        form_title="Бесплатная диагностика — автоматизация процессов",
        form_meta="⏱ ~10 минут · Результат: карта процессов для автоматизации и рекомендации — на email. Бесплатно.",
        submit_label="Отправить анкету",
        api_endpoint="/api/diagnostics/free",
    )


@router.get("/diagnostics/your-business/", response_class=HTMLResponse)
async def diagnostics_your_business(request: Request) -> HTMLResponse:
    """«Ваше дело» — форма анкеты для ИП/блогеров."""
    return _diagnostics_form(
        request,
        form_kind="your_business",
        form_title="Ваше дело — болевые точки предпринимателя",
        form_meta="⏱ 5 минут · 8 вопросов · Результат: чек-лист решений под вашу ситуацию — на email. Бесплатно.",
        submit_label="Получить чек-лист",
        api_endpoint="/api/diagnostics/your-business",
    )


@router.get("/diagnostics/paid-form/", response_class=HTMLResponse)
async def diagnostics_paid_form(
    request: Request, payment: str = ""
) -> HTMLResponse:
    """Форма платной диагностики (после оплаты 3 000 ₽)."""
    return _diagnostics_form(
        request,
        form_kind="paid",
        form_title="Анализ процессов предприятия",
        form_meta="⏱ 15–20 минут · Результат: PDF-отчёт 5–10 стр. — зрелость процессов, комплаенс 152-ФЗ/168-ФЗ, дорожная карта на 1/3/6 месяцев.",
        submit_label="Отправить и получить отчёт",
        api_endpoint="/api/diagnostics/paid",
        payment=payment,
    )



@router.get("/diagnostics/unsubscribe/", response_class=HTMLResponse)
async def diagnostics_unsubscribe(request: Request, id: int = 0, t: str = "") -> HTMLResponse:
    """Отписка от email-цепочки диагностик (HMAC-ссылка из письма, B4)."""
    from app.diagnostics_followup import verify_unsub
    from app.diagnostics_store import set_unsubscribed

    if id > 0 and verify_unsub(id, t):
        set_unsubscribed(id)
        return _site_page(
            request,
            h1="Вы отписаны",
            description="Письма цепочки АЛТ больше не придут на ваш email.",
            hint="Диагностику и консультации всегда можно пройти снова на rusalts.ru",
        )
    return _site_page(
        request,
        h1="Ссылка недействительна",
        description="Проверьте ссылку из письма — возможно, она скопирована не полностью.",
    )


@router.get("/for/{segment}/", response_class=HTMLResponse)
async def segment_landing(request: Request, segment: str) -> HTMLResponse:
    """Посадочные под сегменты (B6): блогеру / предпринимателю / сообществу."""
    from app.segments import SEGMENTS

    seg = SEGMENTS.get(segment)
    if not seg:
        raise HTTPException(status_code=404, detail="Сегмент не найден")
    return templates.TemplateResponse(
        request,
        "segment_page.html",
        {
            "vk_app_id": config.VK_APP_ID or None,
            "is_widget": False,
            "layout_class": "layout-site",
            "page_title": seg["title"],
            "seg": seg,
        },
    )


@router.get("/channels/", response_class=HTMLResponse)
async def channels(request: Request) -> HTMLResponse:
    return _site_page(
        request,
        h1="Каналы",
        description="Публичная страница: взаимодействие с мессенджерами и соцсетями.",
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
