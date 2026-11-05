"""Точка входа: веб-интерфейс консультанта и API для дальнейшего развития."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app import config
from app.middleware import EmbedSecurityMiddleware
from app.observability import RequestIdMiddleware
from app.routers import admin, api, billing_admin, content, diagnostics, pages, parser, request_form, sandbox_shop, talk

config.ensure_data_dirs()


app = FastAPI(
    title="Консультант (прототип)",
    description="Интерфейс вопрос–ответ и шаблоны документов; готовится к интеграции с VK и LLM.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)
app.add_middleware(RequestIdMiddleware)
app.add_middleware(EmbedSecurityMiddleware)

# ─── Фоновый цикл email-цепочки диагностик (B4) ───────────────────────────
import asyncio  # noqa: E402
import logging  # noqa: E402
import time  # noqa: E402

_followup_logger = logging.getLogger("diagnostics_followup")


async def _followup_loop() -> None:
    from app.diagnostics_followup import followup_tick
    from app.diagnostics_store import cleanup_expired

    last_cleanup = 0.0
    while True:
        try:
            followup_tick()
            if time.monotonic() - last_cleanup > 86400:  # раз в сутки — очистка по срокам
                cleaned = cleanup_expired()
                if any(cleaned.values()):
                    _followup_logger.info("pdn_retention_cleanup", **cleaned)
                last_cleanup = time.monotonic()
        except Exception as e:  # цикл не должен умирать
            _followup_logger.warning("followup loop error: %s", e)
        await asyncio.sleep(3600)


@app.on_event("startup")
async def _start_followup_loop() -> None:
    # Сидинг тарифных планов (ТЗ Купец v2 §7): idempotent, правки админки не трогает
    try:
        from app.tariffs_store import seed_defined_products
        seed_defined_products()
    except Exception:
        pass
    app.state.followup_task = asyncio.create_task(_followup_loop())
    app.state.consistency_task = asyncio.create_task(_consistency_loop())


async def _consistency_loop() -> None:
    """Ночной чек согласованности (E2): ежедневно 04:30 МСК, расхождения — алерт в MAX."""
    from datetime import datetime, timedelta, timezone

    MSK = timezone(timedelta(hours=3))
    app.state.consistency_report = None
    while True:
        now = datetime.now(MSK)
        run_at = now.replace(hour=4, minute=30, second=0, microsecond=0)
        if run_at <= now:
            run_at += timedelta(days=1)
        await asyncio.sleep((run_at - now).total_seconds())
        try:
            from app.consistency_check import format_alert, run_consistency_check
            from app.max_notify import notify_max

            report = await run_consistency_check()
            app.state.consistency_report = report
            if not report["ok"]:
                await notify_max(format_alert(report))
        except Exception as e:  # цикл не должен умирать
            _followup_logger.warning("consistency loop error: %s", e)


app.include_router(pages.router)
app.include_router(admin.router)
app.include_router(api.router)
app.include_router(content.router)
app.include_router(diagnostics.router)
app.include_router(parser.router)
app.include_router(talk.router)
app.include_router(request_form.router)
app.include_router(billing_admin.router)
app.include_router(sandbox_shop.router)

static_dir = config.BASE_DIR / "app" / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Загруженная графика (отзывы и пр.) — в data/, rsync при деплое не удаляет
uploads_dir = config.DATA_DIR / "uploads"
uploads_dir.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(uploads_dir)), name="uploads")

# /talk assets are served from repository /talk/public (HTML is served by a route)
talk_public_dir = config.BASE_DIR / "talk" / "public"
app.mount("/talk/assets", StaticFiles(directory=str(talk_public_dir)), name="talk_assets")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
