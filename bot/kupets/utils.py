"""Общие утилиты: логирование, user-agent, форматирование."""

from __future__ import annotations

import logging
import random
from typing import Any

import structlog

from config import Config

USER_AGENTS: list[str] = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) "
        "Gecko/20100101 Firefox/126.0"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.5 Safari/605.1.15"
    ),
]


def setup_logging(level: str | None = None) -> None:
    """Настраивает structlog + stdlib logging."""
    log_level = (level or Config.LOG_LEVEL).upper()
    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, log_level, logging.INFO),
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, log_level, logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "kupets") -> Any:
    return structlog.get_logger(name)


def pick_user_agent() -> str:
    return random.choice(USER_AGENTS)


def browser_headers() -> dict[str, str]:
    return {
        "User-Agent": pick_user_agent(),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        ),
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Origin": "https://www.ozon.ru",
        "Referer": "https://www.ozon.ru/",
        "DNT": "1",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
    }


def format_price(value: float) -> str:
    """18 990 ₽ — с неразрывным пробелом как разделителем тысяч."""
    n = int(round(value))
    s = f"{n:,}".replace(",", "\u202f")
    return f"{s} ₽"
