from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app import config


@dataclass(frozen=True)
class RouterAIUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    model: str | None = None


class RouterAIError(RuntimeError):
    pass


def _join_openai_path(base_url: str, path: str) -> str:
    """
    RouterAI позиционируется как OpenAI-compatible, но в окружении могут встречаться:
    - base_url = https://routerai.ru/api
    - base_url = https://routerai.ru/api/v1
    Чтобы не получить /v1/v1/*, нормализуем путь.
    """
    b = (base_url or "").rstrip("/")
    p = "/" + path.lstrip("/")
    if b.endswith("/v1") and p.startswith("/v1/"):
        p = p.removeprefix("/v1")
    return f"{b}{p}"


async def chat_completion(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    timeout_s: float = 20.0,
) -> tuple[str, RouterAIUsage, dict[str, Any]]:
    if not base_url or not api_key:
        raise RouterAIError("RouterAI is not configured")

    url = _join_openai_path(base_url, "/v1/chat/completions")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
    }
    connect_s = float(getattr(config, "ROUTERAI_TIMEOUT_CONNECT_S", 15.0))
    read_s = float(timeout_s)
    httpx_timeout = httpx.Timeout(
        connect=connect_s,
        read=read_s,
        write=min(read_s, 600.0),
        pool=10.0,
    )
    async with httpx.AsyncClient(timeout=httpx_timeout) as client:
        try:
            r = await client.post(url, headers=headers, json=payload)
        except Exception as e:  # noqa: BLE001
            # Some httpx exceptions stringify to empty string; keep type + repr for diagnostics.
            raise RouterAIError(
                f"RouterAI request failed ({type(e).__name__}) to {url}: {e!r}"
            ) from e

    if r.status_code >= 400:
        body = (r.text or "").strip()
        raise RouterAIError(f"RouterAI HTTP {r.status_code} from {url}: {body[:2000]}")

    data = r.json()
    content = ""
    try:
        content = data["choices"][0]["message"]["content"]
    except Exception as e:  # noqa: BLE001
        raise RouterAIError("Unexpected RouterAI response shape") from e

    usage_raw = data.get("usage") or {}
    usage = RouterAIUsage(
        input_tokens=usage_raw.get("prompt_tokens"),
        output_tokens=usage_raw.get("completion_tokens"),
        cost_usd=usage_raw.get("cost") if isinstance(usage_raw.get("cost"), (int, float)) else None,
        model=data.get("model") or model,
    )
    return str(content), usage, data

