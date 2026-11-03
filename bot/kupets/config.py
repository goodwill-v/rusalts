"""Загрузка конфигурации Купца из .env."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(_ENV_PATH)


def _bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


class Config:
    """Конфиг приложения. Секреты только из окружения."""

    KUPETS_CLIENT_ID: str = os.getenv("KUPETS_CLIENT_ID", "")
    KUPETS_CLIENT_SECRET: str = os.getenv("KUPETS_CLIENT_SECRET", "")
    KUPETS_API_KEY: str = os.getenv("KUPETS_API_KEY", "")
    OZON_CLIENT_ID: str = os.getenv("OZON_CLIENT_ID", "")
    OZON_API_KEY: str = os.getenv("OZON_API_KEY", "")

    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")

    ROUTERAI_API_KEY: str = os.getenv("ROUTERAI_API_KEY", "")
    ROUTERAI_BASE_URL: str = os.getenv(
        "ROUTERAI_BASE_URL", "https://routerai.ru/api/v1"
    )
    ROUTERAI_MODEL: str = os.getenv(
        "ROUTERAI_MODEL", "deepseek/deepseek-v4-flash"
    )
    ROUTERAI_VISION_MODEL: str = os.getenv(
        "ROUTERAI_VISION_MODEL", "qwen/qwen3.7-flash"
    )

    PARSING_DELAY_MS: int = int(os.getenv("PARSING_DELAY_MS", "400"))
    PARSING_MAX_RETRIES: int = int(os.getenv("PARSING_MAX_RETRIES", "3"))
    PARSING_USE_PROXY: bool = _bool("PARSING_USE_PROXY", "false")
    PARSING_PROXY_URL: str = os.getenv("PARSING_PROXY_URL", "")
    PARSING_CONCURRENCY: int = int(os.getenv("PARSING_CONCURRENCY", "3"))

    # Firecrawl — внешний поиск (обход Composer API)
    FIRECRAWL_API_KEY: str = os.getenv("FIRECRAWL_API_KEY", "")
    TAVILY_API_KEY: str = os.getenv("TAVILY_API_KEY", "")
    ENABLE_FIRECRAWL: bool = _bool("ENABLE_FIRECRAWL", "true")

    REDIS_URL: str = os.getenv("REDIS_URL", "")
    CACHE_TTL_SECONDS: int = int(os.getenv("CACHE_TTL_SECONDS", "300"))

    VISION_ENABLED: bool = _bool("VISION_ENABLED", "true")
    DRY_RUN: bool = _bool("DRY_RUN", "true")
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

    # Пробовать карточки /product/ (часто 403) — по умолчанию нет
    TRY_PRODUCT_DETAIL: bool = _bool("TRY_PRODUCT_DETAIL", "false")
    USE_BROWSER: bool = _bool("USE_BROWSER", "false")
    SEARCH_PAGES: int = int(os.getenv("SEARCH_PAGES", "2"))

    # Почта для жалоб
    REPORT_EMAIL_TO: str = os.getenv("REPORT_EMAIL_TO", "v.devops@yandex.ru")
    SMTP_HOST: str = os.getenv("SMTP_HOST", "smtp.yandex.ru")
    SMTP_PORT: int = int(os.getenv("SMTP_PORT", "587"))
    SMTP_USER: str = os.getenv("SMTP_USER", "")
    SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD", "")
    SMTP_FROM: str = os.getenv("SMTP_FROM", "")
    SMTP_USE_SSL: bool = _bool("SMTP_USE_SSL", "false")

    # Лимиты пайплайна
    SEARCH_ITEMS_LIMIT: int = int(os.getenv("SEARCH_ITEMS_LIMIT", "36"))
    ENRICH_PRODUCTS_LIMIT: int = int(os.getenv("ENRICH_PRODUCTS_LIMIT", "24"))
    ENRICH_SELLERS_LIMIT: int = int(os.getenv("ENRICH_SELLERS_LIMIT", "8"))
    TOP_RESULTS: int = 10
    NEAR_MISS_LIMIT: int = 3
