"""Точка входа: веб-интерфейс консультанта и API для дальнейшего развития."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app import config
from app.middleware import EmbedSecurityMiddleware
from app.observability import RequestIdMiddleware
from app.routers import api, content, pages, parser, talk

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

app.include_router(pages.router)
app.include_router(api.router)
app.include_router(content.router)
app.include_router(parser.router)
app.include_router(talk.router)

static_dir = config.BASE_DIR / "app" / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# /talk assets are served from repository /talk/public (HTML is served by a route)
talk_public_dir = config.BASE_DIR / "talk" / "public"
app.mount("/talk/assets", StaticFiles(directory=str(talk_public_dir)), name="talk_assets")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
