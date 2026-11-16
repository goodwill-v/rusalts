"""Зрение для цен (поручение Шефа 16.11): скрин выдачи Ozon/WB → карточки.

Как работает: пользователь присылает снимок выдачи маркетплейса (например,
категории Ozon с ценами) — RouterAI vision (deepseek-v4.1-flash, модальность
«Изображение») читает карточки и возвращает JSON: name, brand, price,
old_price, discount_pct, rating, reviews. Поверх — вердикты
«справедливости скидки» (fairness_verdicts): настоящая скидка или
«легенда», дорого/дёшево против медианы.

PoC 16.11: скрин Шефа распознан 4/4 карточки (max_tokens 5000 — ризонер
тратит бюджет на размышления; при 2000 ответ не успевал).

Чистые функции (parse_cards_json, fairness_verdicts) — тестируются в CI.
"""

from __future__ import annotations

import base64
import json
import re
import statistics

from config import Config
from utils import get_logger

logger = get_logger("parser.vision_prices")

VISION_MODEL = "deepseek/deepseek-v4.1-flash"
VISION_MAX_TOKENS = 5000

EXTRACT_PROMPT = (
    "На скриншоте страница маркетплейса с карточками товаров. Для КАЖДОЙ "
    "видимой карточки товара извлеки: name (название), brand (бренд, если "
    "виден), price (текущая цена числом), old_price (перечёркнутая цена "
    "числом или null), discount_pct (скидка в процентах числом или null), "
    "rating (оценка числом или null), reviews (число отзывов числом или "
    "null). Ответ — строго JSON-массив объектов, без пояснений."
)

PRICE_CAPTION_RE = re.compile(
    r"(цены|скан|scan|выдач\w+ с ценам|сколько стоит|сравни цены)", re.IGNORECASE
)


def should_prices_caption(caption: str | None) -> bool:
    """Подпись к снимку просит чтение цен? («цены», «скан», «сколько стоит»)."""
    return bool(caption and PRICE_CAPTION_RE.search(caption))


def parse_cards_json(text: str) -> list[dict]:
    """Ответ модели → список карточек-словарей. Терпит ```json-заборы,
    текст до/после массива и кривые записи чисел."""
    if not text:
        return []
    i, j = text.find("["), text.rfind("]")
    if i < 0 or j <= i:
        return []
    candidate = text[i:j + 1]
    try:
        arr = json.loads(candidate)
    except json.JSONDecodeError:
        # починка хвостовых запятых и одинарных кавычек
        fixed = re.sub(r",\s*([\]}])", r"\1", candidate).replace("'", '"')
        try:
            arr = json.loads(fixed)
        except json.JSONDecodeError:
            return []
    cards = []
    for c in arr if isinstance(arr, list) else []:
        if not isinstance(c, dict):
            continue
        cards.append(_norm_card(c))
    return cards


def _num(v) -> float | None:
    if v in (None, "", "—"):
        return None
    try:
        return float(re.sub(r"[^\d.]", "", str(v).replace(",", ".")))
    except (TypeError, ValueError):
        return None


def _norm_card(c: dict) -> dict:
    return {
        "name": str(c.get("name") or "").strip(),
        "brand": (str(c.get("brand")).strip() if c.get("brand") not in (None, "null") else ""),
        "price": _num(c.get("price")) or 0.0,
        "old_price": _num(c.get("old_price")),
        "discount_pct": _num(c.get("discount_pct")),
        "rating": _num(c.get("rating")),
        "reviews_raw": c.get("reviews"),
        "reviews": int(_num(c.get("reviews")) or 0),
    }


async def run_vision_cards(image_bytes: bytes, *, client=None) -> list[dict]:
    """Асинхронный вызов vision-модели → карточки (см. parse_cards_json)."""
    from openai import AsyncOpenAI

    api_key = Config.ROUTERAI_API_KEY
    if not api_key:
        raise RuntimeError("routerai_key_missing")
    ai = client or AsyncOpenAI(
        api_key=api_key,
        base_url=Config.ROUTERAI_BASE_URL,
        timeout=180,
        max_retries=1,
    )
    b64 = base64.b64encode(image_bytes).decode()
    resp = await ai.chat.completions.create(
        model=VISION_MODEL,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": EXTRACT_PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]}],
        max_tokens=VISION_MAX_TOKENS,
        temperature=0,
    )
    msg = resp.choices[0].message
    text = (msg.content or "").strip() or (getattr(msg, "reasoning", None) or "").strip()
    cards = parse_cards_json(text)
    logger.info("vision_cards_parsed", extra={"cards": len(cards)})
    return cards


# ─── Справедливость скидки ───────────────────────────────────────────


def fairness_verdicts(cards: list[dict]) -> list[dict]:
    """На каждую карточку — вердикт честности: level ok/warn/danger + причины.

    Правила:
    - «легенда скидки»: old_price есть, price/old_price < 0.2 (скидка >80%) —
      старая цена обычно оторвана от рынка (типовой трюк перед распродажей);
    - дешевле медианы на 40%+ — подозрительно дёшево;
    - дороже медианы на 50%+ — заметно дороже рынка;
    - reviews >= 100 и rating >= 4.5 — «отзывы подтверждены» (плюс доверия).
    """
    prices = [c["price"] for c in cards if c.get("price", 0) > 0]
    med = statistics.median(prices) if len(prices) >= 2 else 0.0
    out = []
    for c in cards:
        reasons: list[str] = []
        level = "ok"
        price, old = c.get("price", 0) or 0, c.get("old_price")
        if old and old > 0 and price > 0 and price / old < 0.2:
            level = "danger"
            reasons.append(
                f"скидка-легенда: было {int(old)}₽, стало {int(price)}₽ — "
                "старая цена оторвана от рынка"
            )
        if med > 0 and price > 0:
            if price < med * 0.6 and level == "ok":
                level = "warn"
                reasons.append(f"заметно дешевле медианы ({int(med)}₽) — проверяйте продавца")
            elif price > med * 1.5:
                reasons.append(f"дороже медианы на {int((price/med - 1) * 100)}%")
        if c.get("reviews", 0) >= 100 and (c.get("rating") or 0) >= 4.5:
            reasons.append("отзывы подтверждены (много, рейтинг высокий)")
        out.append({"card": c, "level": level, "reasons": reasons})
    return out
