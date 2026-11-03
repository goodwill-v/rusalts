"""Расчёт интегральной оценки SCORE."""

from __future__ import annotations

import math

from engine.normalizer import median
from engine.weights import SOFT_PENALTY_POINTS, WEIGHTS
from models.product import ProductData
from models.result import ScoreBreakdown, ScoredProduct, SearchContext
from utils import get_logger

logger = get_logger("engine.scorer")


class ProductScorer:
    """Расчёт SCORE. Сумма весов = 1.0 → шкала 0–100."""

    def score_all(
        self,
        products: list[ProductData],
        *,
        soft_penalty: bool = False,
        context: SearchContext | None = None,
    ) -> list[ScoredProduct]:
        if not products:
            return []

        prices = [p.price for p in products]
        if context is None:
            from models.result import SearchFilters

            ctx = SearchContext(
                all_prices=prices,
                median_price=median(prices),
                filters=SearchFilters(text=""),
            )
        else:
            ctx = context

        scored: list[ScoredProduct] = []
        for p in products:
            sp = self.score(p, ctx, soft_penalty=soft_penalty)
            scored.append(sp)
            logger.debug(
                "scored",
                product=p.name,
                score=sp.score,
                breakdown=sp.breakdown.__dict__,
            )

        scored.sort(key=lambda x: x.score, reverse=True)
        return scored

    def score(
        self,
        product: ProductData,
        context: SearchContext,
        *,
        soft_penalty: bool = False,
    ) -> ScoredProduct:
        s_seller = self._score_seller(product)
        s_price = self._score_price(product, context)
        s_reviews = self._score_reviews(product)
        s_delivery = self._score_delivery(product)

        pts_seller = WEIGHTS["seller"] * s_seller * 100
        pts_price = WEIGHTS["price"] * s_price * 100
        pts_reviews = WEIGHTS["reviews"] * s_reviews * 100
        pts_delivery = WEIGHTS["delivery"] * s_delivery * 100
        penalty = SOFT_PENALTY_POINTS if soft_penalty else 0.0

        raw = pts_seller + pts_price + pts_reviews + pts_delivery - penalty
        total = int(round(max(0.0, min(100.0, raw))))

        breakdown = ScoreBreakdown(
            seller=round(pts_seller, 1),
            price=round(pts_price, 1),
            reviews=round(pts_reviews, 1),
            delivery=round(pts_delivery, 1),
            soft_penalty=penalty,
        )

        notes = self._match_notes(product, context)
        return ScoredProduct(
            product=product,
            score=total,
            breakdown=breakdown,
            match_notes=notes,
        )

    def _score_seller(self, product: ProductData) -> float:
        s = product.seller
        if not s or s.rating is None:
            return 0.3
        score = s.rating / 5.0
        if s.is_verified:
            score += 0.1
        if s.orders_count and s.orders_count > 1000:
            score += 0.1
        return min(score, 1.0)

    def _score_price(self, product: ProductData, context: SearchContext) -> float:
        # Неизвестная цена («цена по ссылке») не участвует в сравнении:
        # нейтральный балл вместо ложного «самый дешёвый» (price=0).
        prices = sorted(p for p in context.all_prices if p > 0)
        if product.price <= 0 or len(prices) < 2:
            return 0.5
        # позиция в отсортированном списке (инвертировано: дешевле → выше)
        # при дубликатах берём первое вхождение
        try:
            idx = prices.index(product.price)
        except ValueError:
            idx = len(prices) // 2
        return 1.0 - (idx / (len(prices) - 1))

    def _score_reviews(self, product: ProductData) -> float:
        score = 0.0
        if product.rating is not None:
            score += (product.rating / 5.0) * 0.5
        if product.reviews_count > 0:
            log_bonus = min(math.log10(product.reviews_count) / 4, 0.5)
            score += log_bonus
        return min(score, 1.0)

    def _score_delivery(self, product: ProductData) -> float:
        score = 0.5
        if product.delivery_type == "FBO":
            score += 0.3
        if product.delivery_price == 0:
            score += 0.2
        if product.delivery_days is not None:
            if product.delivery_days <= 1:
                score += 0.2
            elif product.delivery_days <= 3:
                score += 0.1
        return min(score, 1.0)

    def _match_notes(
        self, product: ProductData, context: SearchContext
    ) -> list[str]:
        notes: list[str] = []
        f = context.filters
        parts: list[str] = []
        if product.ram_gb is not None:
            parts.append(f"{product.ram_gb:g}ГБ ОЗУ")
        if product.storage_gb is not None:
            parts.append(f"{product.storage_gb:g}ГБ")
        if product.screen_inches is not None:
            parts.append(f'экран {product.screen_inches:g}"')

        if f and any(
            x is not None for x in (f.ram_gb, f.storage_gb, f.screen_inches)
        ):
            ok = True
            if f.ram_gb and (product.ram_gb is None or product.ram_gb < f.ram_gb):
                ok = False
            if f.storage_gb and (
                product.storage_gb is None or product.storage_gb < f.storage_gb
            ):
                ok = False
            if f.screen_inches and (
                product.screen_inches is None
                or product.screen_inches < f.screen_inches
            ):
                ok = False
            if ok and parts:
                notes.append(", ".join(parts) + " — все характеристики совпадают")
            elif product.near_miss_reasons:
                notes.extend(product.near_miss_reasons)
            elif product.rejection_reasons:
                notes.extend(product.rejection_reasons)
        elif parts:
            notes.append(", ".join(parts))

        if context.median_price > 0:
            diff = (product.price - context.median_price) / context.median_price
            if diff < -0.1:
                notes.append(
                    f"цена ниже медианы на {abs(diff)*100:.0f}%"
                )
            elif diff > 0.15:
                notes.append(f"цена выше медианы на {diff*100:.0f}%")

        if product.reviews_count < 100 and product.rating is not None:
            notes.append("мало отзывов")

        return notes
