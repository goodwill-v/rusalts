"""Распознавание товара на фото через RouterAI VLM → текстовый запрос."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass

import httpx
from openai import AsyncOpenAI

from config import Config
from utils import get_logger

logger = get_logger("vision.recognizer")

_SYSTEM = (
    "Ты помощник поиска товаров на Ozon. "
    "По фото определи товар и сформулируй короткий поисковый запрос на русском. "
    "Отвечай только JSON без markdown."
)

_USER_TEMPLATE = (
    "Опиши товар на фото для поиска на маркетплейсе Ozon.\n"
    "Верни JSON строго такого вида:\n"
    '{{"query":"поисковая строка","brand":"бренд или пусто",'
    '"category":"категория","confidence":"high|medium|low",'
    '"notes":"кратко что видно"}}\n'
    "query — 3–12 слов: тип товара, бренд/модель если видно, цвет, ключевые признаки. "
    "Без слов «фото», «картинка», «найди».\n"
    "{hint}"
)


@dataclass
class PhotoRecognition:
    query: str
    brand: str = ""
    category: str = ""
    confidence: str = "medium"
    notes: str = ""
    raw: str = ""


class PhotoRecognizer:
    def __init__(self) -> None:
        self._client: AsyncOpenAI | None = None
        if Config.ROUTERAI_API_KEY and not Config.DRY_RUN:
            self._client = AsyncOpenAI(
                api_key=Config.ROUTERAI_API_KEY,
                base_url=Config.ROUTERAI_BASE_URL,
                timeout=httpx.Timeout(45.0, connect=10.0),
                max_retries=1,
            )

    @property
    def available(self) -> bool:
        return (
            Config.VISION_ENABLED
            and not Config.DRY_RUN
            and self._client is not None
        )

    async def recognize(
        self,
        image_bytes: bytes,
        *,
        caption: str | None = None,
        mime: str = "image/jpeg",
    ) -> PhotoRecognition:
        if not image_bytes:
            raise ValueError("empty image")
        if not self.available:
            raise RuntimeError("vision_not_available")

        assert self._client is not None
        b64 = base64.b64encode(image_bytes).decode("ascii")
        data_url = f"data:{mime};base64,{b64}"
        hint = ""
        if caption and caption.strip():
            hint = f"Подсказка пользователя: {caption.strip()}\n"

        resp = await self._client.chat.completions.create(
            model=Config.ROUTERAI_VISION_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": _USER_TEMPLATE.format(hint=hint),
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": data_url},
                        },
                    ],
                },
            ],
            max_tokens=300,
            temperature=0.2,
        )
        err = getattr(resp, "error", None)
        if err:
            raise RuntimeError(f"vision_api_error: {err}")
        if not resp.choices:
            raise RuntimeError("vision_api_empty_choices")
        msg = resp.choices[0].message
        text = (msg.content or "").strip()
        if not text:
            text = (getattr(msg, "reasoning", None) or "").strip()
        result = parse_vision_response(text)
        logger.info(
            "photo_recognized",
            query=result.query,
            confidence=result.confidence,
            model=Config.ROUTERAI_VISION_MODEL,
        )
        return result


def parse_vision_response(text: str) -> PhotoRecognition:
    """Достаёт query из JSON или берёт первую осмысленную строку."""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty_vision_response")

    data = _extract_json(raw)
    if data:
        query = str(data.get("query") or "").strip()
        if query:
            return PhotoRecognition(
                query=_clean_query(query),
                brand=str(data.get("brand") or "").strip(),
                category=str(data.get("category") or "").strip(),
                confidence=str(data.get("confidence") or "medium").strip().lower(),
                notes=str(data.get("notes") or "").strip(),
                raw=raw,
            )

    # fallback: первая непустая строка без markdown-обёртки
    line = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.I | re.M).strip()
    for candidate in line.splitlines():
        candidate = candidate.strip().strip('"').strip("'")
        if candidate and not candidate.startswith("{") and len(candidate) > 2:
            return PhotoRecognition(query=_clean_query(candidate), raw=raw)
    raise ValueError("cannot_parse_vision_response")


def _extract_json(text: str) -> dict | None:
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", cleaned, re.I)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        obj = json.loads(cleaned)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[\s\S]*\}", cleaned)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _clean_query(query: str) -> str:
    q = re.sub(r"\s+", " ", query).strip(" .,;:\"'")
    # убрать типичный мусор от LLM
    for prefix in ("найди ", "найти ", "ищу ", "поиск "):
        if q.lower().startswith(prefix):
            q = q[len(prefix) :].strip()
    return q[:200]
