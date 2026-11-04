"""AI-объяснения топ-3 через RouterAI (batch)."""

from __future__ import annotations

import httpx
from openai import AsyncOpenAI

from config import Config
from models.result import ScoredProduct, SearchContext
from utils import get_logger

logger = get_logger("ai.explainer")


class AIExplainer:
    def __init__(self) -> None:
        self._client: AsyncOpenAI | None = None
        if Config.ROUTERAI_API_KEY and not Config.DRY_RUN:
            # Актуальный endpoint: https://routerai.ru/api/v1
            # (старый api.routerai.ru — NXDOMAIN)
            self._client = AsyncOpenAI(
                api_key=Config.ROUTERAI_API_KEY,
                base_url=Config.ROUTERAI_BASE_URL,
                timeout=httpx.Timeout(20.0, connect=8.0),
                max_retries=1,
            )

    async def explain_top3(
        self,
        products: list[ScoredProduct],
        context: SearchContext,
    ) -> list[str]:
        if not products:
            return []

        if Config.DRY_RUN or self._client is None:
            return [self._fallback_verdict(p) for p in products[:3]]

        try:
            return await self._batch_explain(products[:3], context)
        except Exception as exc:
            logger.warning("ai_explainer_failed", error=str(exc))
            return [self._fallback_verdict(p) for p in products[:3]]

    async def _batch_explain(
        self,
        products: list[ScoredProduct],
        context: SearchContext,
    ) -> list[str]:
        assert self._client is not None
        lines = []
        for i, sp in enumerate(products, 1):
            p = sp.product
            seller_rating = p.seller.rating if p.seller else None
            lines.append(
                f"{i}. Товар: {p.name}\n"
                f"   Цена: {p.price} руб. (медиана: {context.median_price:.0f})\n"
                f"   Рейтинг: {p.rating}/5, отзывов: {p.reviews_count}\n"
                f"   Продавец: {p.seller_name}, рейтинг: {seller_rating}\n"
                f"   SCORE: {sp.score}/100 "
                f"(продавец {sp.breakdown.seller:.0f}, "
                f"цена {sp.breakdown.price:.0f}, "
                f"отзывы {sp.breakdown.reviews:.0f}, "
                f"доставка {sp.breakdown.delivery:.0f})\n"
                f"   Заметки: {'; '.join(sp.match_notes) or '—'}"
            )
        prompt = (
            "По каждому товару дай одну короткую строку-вердикт (почему "
            "стоит/не стоит брать), опираясь только на факты. "
            "Без слова «идеальный». Формат ответа строго:\n"
            "1) ...\n2) ...\n3) ...\n\n"
            + "\n\n".join(lines)
        )
        resp = await self._client.chat.completions.create(
            model=Config.ROUTERAI_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Ты — консультант по выбору товаров. "
                        "Отвечай кратко, по-русски. Без маркдауна."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            max_tokens=400,
            temperature=0.3,
        )
        msg = resp.choices[0].message
        text = (msg.content or "").strip()
        if not text:
            # некоторые модели кладут ответ в reasoning
            text = (getattr(msg, "reasoning", None) or "").strip()
        parsed = self._parse_numbered(text, len(products))
        if len(parsed) < len(products):
            while len(parsed) < len(products):
                parsed.append(self._fallback_verdict(products[len(parsed)]))
        return parsed

    async def explain_compare(self, a: dict, b: dict) -> str:
        """Итог сравнения двух товаров (фича X vs Y, ТЗ v2): 2–3 короткие строки.

        a/b: {name, price, rating, reviews, marketplace}. При сбое — вычисляемый
        вердикт из services.compare (вызывающий передаёт fallback вторым вызовом).
        """
        if self._client is None:
            raise RuntimeError("routerai_unavailable")

        def fmt(d: dict) -> str:
            price = f"{int(d['price'])}₽" if d.get("price") else "цена не видна"
            rating = f"{d['rating']}/5" if d.get("rating") else "без рейтинга"
            reviews = f"{d.get('reviews') or 0} отзывов"
            return (
                f"- {d.get('name', '')[:70]} ({d.get('marketplace', '')}): "
                f"{price}, {rating}, {reviews}"
            )

        prompt = (
            "Сравни два товара для покупателя (сравниваем объективно: цена, "
            "рейтинг, отзывы). Ответ — 2–3 короткие строки по-русски без "
            "маркдауна, последняя строка — кому какой лучше подходит:\n"
            f"Товар А: {fmt(a)}\nТовар Б: {fmt(b)}"
        )
        resp = await self._client.chat.completions.create(
            model=Config.ROUTERAI_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": "Ты — консультант по выбору товаров. Кратко, по-русски.",
                },
                {"role": "user", "content": prompt},
            ],
            max_tokens=250,
            temperature=0.3,
        )
        msg = resp.choices[0].message
        text = (msg.content or "").strip()
        return text or (getattr(msg, "reasoning", None) or "").strip()

    def _parse_numbered(self, text: str, n: int) -> list[str]:
        import re

        found = re.findall(
            r"(?:^|\n)\s*(?:\d+)[\)\.\-:]\s*(.+)",
            text,
        )
        if found:
            return [f.strip() for f in found[:n]]
        parts = [p.strip() for p in text.split("\n") if p.strip()]
        return parts[:n]

    def _fallback_verdict(self, product: ScoredProduct) -> str:
        if product.match_notes:
            note = product.match_notes[0]
            if product.score >= 85:
                return f"Сильный вариант: {note}"
            return note
        if product.score >= 85:
            return "Отличный выбор: хорошая цена и надёжный продавец"
        if product.score >= 65:
            return "Достойный вариант, но проверьте альтернативы"
        return "Есть вопросы по качеству — читайте отзывы внимательнее"
