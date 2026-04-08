"""Заголовки для встраивания в VK (iframe) и базовая безопасность."""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app import config


class EmbedSecurityMiddleware(BaseHTTPMiddleware):
    """
    Разрешает отображение в iframe на доменах VK (CSP frame-ancestors).
    Не выставляем X-Frame-Options: DENY — он ломает встраивание.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        # unpkg — CDN vk-bridge; connect-src — запросы API/статистики внутри VK-контейнера
        csp = (
            f"default-src 'self'; "
            f"script-src 'self' https://vk.com https://*.vk.com https://vk.ru https://*.vk.ru "
            f"https://unpkg.com https://*.unpkg.com 'unsafe-inline'; "
            f"style-src 'self' 'unsafe-inline'; "
            f"img-src 'self' data: https:; "
            f"connect-src 'self' https://vk.com https://*.vk.com https://vk.ru https://*.vk.ru "
            f"https://oauth.vk.com https://api.vk.com https://login.vk.com wss://*.vk.com wss://*.vk.ru; "
            f"frame-ancestors {config.FRAME_ANCESTORS}"
        )
        response.headers["Content-Security-Policy"] = csp
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response
