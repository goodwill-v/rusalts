"""AI-объяснения топ-3 через RouterAI (batch) + ИИ-фичи (резюме, сравнение, подарки).

СТРУКТУРА (важно): все методы — внутри класса AIExplainer; модульные хелперы —
только ПОСЛЕ класса. История 06.11: модульный хелпер, вставленный в середину
класса, сделал все последующие методы вложенными — класс «потерял» их, и
поиск падал целиком. Регресс-тест: tests/test_explainer_structure.py.
"""

from __future__ import annotations

import re

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

    def _parse_numbered(self, text: str, n: int) -> list[str]:
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

    async def summarize_report(self, stats_text: str) -> str:
        """Резюме для продавца по статистике ниши (сводка, ТЗ v2 §3.6): 3–5 строк.
        Пустая строка = сбой (вызывающий покажет вычисляемое резюме)."""
        if self._client is None:
            return ""
        prompt = (
            "Ты — аналитик маркетплейсов. Вот статистика ниши для продавца:\n"
            f"{stats_text}\n\n"
            "Дай 3–5 коротких строк-рекомендаций по-русски, без маркдауна: "
            "стоит ли заходить в нишу, с какой ценой (опираясь на медиану), "
            "на чём выделиться, чего избегать. Только факты из статистики, "
            "без выдумок."
        )
        resp = await self._client.chat.completions.create(
            model=Config.ROUTERAI_MODEL,
            messages=[
                {"role": "system", "content": "Ты — аналитик маркетплейсов. Кратко, по-русски, только по данным."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=700,  # ризонер-модель тратит токены на размышления
            temperature=0.3,
        )
        msg = resp.choices[0].message
        raw = (msg.content or "").strip() or (getattr(msg, "reasoning", None) or "").strip()
        return _strip_reasoning_echo(raw)

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

    async def expand_gift(self, description: str, budget: int, n: int = 4) -> list[str]:
        """Подарочный подбор (фича г): «маме до 2000₽» → 3–4 конкретных
        товарных запроса для поиска. Пустой список = сбой (честный отказ)."""
        if self._client is None:
            return []
        prompt = (
            f"Нужно купить подарок: {description}, бюджет {budget}₽. "
            f"Придумай {n} конкретных товарных запросов для поиска на "
            "маркетплейсах (то, что реально введёшь в поисковую строку). "
            "Формат ответа строго:\n1) запрос\n2) запрос\n...\n"
            "Без пояснений, по-русски, каждый запрос 2–4 слова."
        )
        resp = await self._client.chat.completions.create(
            model=Config.ROUTERAI_MODEL,
            messages=[
                {"role": "system", "content": "Ты — помощник по подаркам. Кратко, по-русски."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=200,
            temperature=0.6,
        )
        msg = resp.choices[0].message
        text = (msg.content or "").strip() or (getattr(msg, "reasoning", None) or "")
        from services.gift import queries_from_llm

        return queries_from_llm(text, n)


def _strip_reasoning_echo(text: str) -> str:
    """Ризонер-модели выводят цепочку размышлений («Сейчас нужно…»,
    «Пользователь просит…», «Анализирую…») перед рекомендациями —
    оставляем финальный блок (от «Рекомендации/Итог/…» или последние 5 строк)."""
    if not text:
        return text
    meta_starts = (
        "Сейчас нужно", "Пользователь просит", "Анализирую", "Данные:",
        "Нужно ответить", "Нужно дать", "Хорошо", "Итак",
    )
    lines = [
        l for l in text.splitlines()
        if l.strip() and not any(l.lstrip().startswith(m) for m in meta_starts)
    ]
    for i, l in enumerate(lines):
        low = l.lstrip().lower()
        if low.startswith(("рекомендации", "итог", "вывод", "резюме")):
            lines = lines[i:]
            break
    return "\n".join(lines[-5:]).strip()
