"""HTTP-клиент к OpenClaw Gateway (Алик) через nginx-мост — таб «Алик» в /zcode.

ТЗ-дополнение §1 + техдокумент Алика (`ALIK_ZCODE_WEB_INTEGRATION_to_KOR.md`):
OpenAI-compatible `/v1/chat/completions`, SSE-стрим. Стабильная сессия — поле
`"user": "web:alik:<chat_id>"` (рекомендация Алика §2.4; заголовок
X-OpenClaw-Session-Key НЕ используем).

СТОП: сам gateway (127.0.0.1:19001) и его службы не трогаем и не рестартуем —
только штатный HTTP API через мост Алика (nginx location /openclaw/).
"""

from __future__ import annotations

import json
import os
from typing import AsyncIterator

import httpx

# Модель openclaw = сам Алик (default agent); другие агенты — отдельная задача (ТЗ §6).
MODEL = os.getenv("ALIK_MODEL", "openclaw")
SEND_TIMEOUT_S = 300.0


def gateway_url() -> str:
    base = os.getenv("ZCODE_UPSTREAM_HTTP_URL", "http://127.0.0.1:42265").rstrip("/")
    return f"{base}/openclaw/v1/chat/completions"


def gateway_token() -> str:
    return os.getenv("OPENCLAW_GATEWAY_TOKEN", "").strip()


class AlikError(RuntimeError):
    """Ошибка вызова gateway (конфигурация, сеть, HTTP-статус)."""


async def stream_chat(messages: list[dict[str, str]], chat_id: str) -> AsyncIterator[str]:
    """Асинхронный генератор текстовых кусков ответа Алика (SSE от gateway).

    messages — массив OpenAI-формата (контекст из БД + новое сообщение).
    """
    token = gateway_token()
    if not token:
        raise AlikError("OPENCLAW_GATEWAY_TOKEN не настроен (.env) — чат с Аликом отключён")
    body = {
        "model": MODEL,
        "messages": messages,
        "stream": True,
        # Стабильная сессия: один ключ на один диалог (Алик §2.4)
        "user": f"web:alik:{chat_id}",
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=SEND_TIMEOUT_S) as client:
            async with client.stream("POST", gateway_url(), json=body, headers=headers) as resp:
                if resp.status_code != 200:
                    raw = (await resp.aread()).decode("utf-8", "replace")
                    raise AlikError(f"gateway HTTP {resp.status_code}: {raw[:200]}")
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        if data == "[DONE]":
                            return
                        continue
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or [{}]
                    delta = (choices[0].get("delta") or {}).get("content")
                    if delta:
                        yield delta
    except httpx.HTTPError as e:
        # 404 = nginx location /openclaw/ ещё не применён (ТЗ §2: честная ошибка)
        raise AlikError(f"gateway недоступен через мост: {e.__class__.__name__}") from e
