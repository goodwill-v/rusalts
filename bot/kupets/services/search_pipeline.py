"""Оркестратор поиска: Firecrawl (3 маркетплейса) → фильтры → SCORE → AI.

Composer API Ozon отключён (ENABLE_COMPOSER=false, по умолчанию): антибот
режет датацентр-IP с сентября 2026. Живой канал — поисковые сниппеты Firecrawl;
ответы кэшируются в SQLite на CACHE_TTL_SEARCH_SECONDS — повторный запрос
не тратит кредиты. Исчерпание лимита — честное сообщение пользователю и
алерты Шефу (services.credit_monitor), а не «Ничего не нашёл».
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from copy import deepcopy

from ai.explainer import AIExplainer
from config import Config
from engine.filter import apply_filters
from engine.junk import filter_junk_items
from engine.normalizer import median
from engine.query_parser import extract_filters
from engine.scorer import ProductScorer
from models.product import ProductData, SearchItem
from models.result import SearchContext, SearchFilters, SearchResponse
from parser.composer import ComposerClient
from parser.firecrawl_search import ProviderQuotaError
from parser.search_channel import search_marketplaces as channel_search
from parser.product import parse_product
from parser.search import parse_search
from parser.seller import attach_sellers
from services import credit_monitor, sqlite_store
from utils import get_logger

logger = get_logger("services.pipeline")

ProgressCallback = Callable[[str], Awaitable[None]]


class SearchPipeline:
    def __init__(self) -> None:
        self.scorer = ProductScorer()
        self.explainer = AIExplainer()

    async def run(
        self,
        query: str,
        on_progress: ProgressCallback | None = None,
        *,
        filters_override: SearchFilters | None = None,
        skip_cache: bool = False,
    ) -> SearchResponse:
        async def progress(text: str) -> None:
            if on_progress:
                try:
                    await on_progress(text)
                except Exception as exc:
                    logger.debug("progress_error", error=str(exc))

        filters = filters_override or extract_filters(query)
        display = filters.text or query
        search_text = self._build_search_text(filters)
        smartphone_query = self._is_smartphone_query(filters, search_text)

        cache_key = self._cache_key(search_text, filters)
        if not skip_cache and not Config.DRY_RUN:
            cached = sqlite_store.cache_get(cache_key)
            if cached is not None:
                logger.info("cache_hit", key=cache_key[:16])
                await progress("✅ Из кэша (свежий за 3 часа)")
                return deepcopy(cached)

        logger.info(
            "pipeline_start",
            query=query,
            search_text=search_text,
            filters=filters.__dict__,
        )

        # Composer мёртв (антибот 403) — по умолчанию не создаём клиент вовсе
        client: ComposerClient | None = None
        if Config.ENABLE_COMPOSER and not Config.DRY_RUN:
            client = ComposerClient()

        try:
            items = await self._search_items(
                search_text, filters, client, progress
            )
            if items is None:
                # Лимит Firecrawl исчерпан — вежливо и честно останавливаем поиск
                notice = credit_monitor.user_notice() or (
                    "🛠 Поиск временно недоступен из-за лимитов сервиса. "
                    "Попробуйте позже."
                )
                await progress(notice)
                resp = SearchResponse(filters=filters, found_total=0)
                resp.provider_limited = True
                resp.query_display = display
                return resp

            if not items:
                await progress("😕 По этим запросам товары не нашлись")
                empty = SearchResponse.empty(filters)
                empty.query_display = display
                return empty

            items, junked = filter_junk_items(
                items, smartphone_query=smartphone_query
            )
            if junked:
                logger.info("junk_filtered", count=len(junked))

            if not items:
                await progress("😕 После отсева мусора ничего не осталось")
                resp = SearchResponse.empty(filters)
                resp.found_total = len(junked)
                resp.query_display = display
                return resp

            await progress(f"⏳ Нашёл {len(items)} товаров, анализирую...")

            prelim = self.pre_rank(items)
            to_enrich = prelim[: Config.ENRICH_PRODUCTS_LIMIT]
            products = await self._enrich(to_enrich, client)

            await progress("⏳ Считаю SCORE...")
            products = await attach_sellers(products, client=client)

            buckets = apply_filters(
                products,
                filters,
                smartphone_query=smartphone_query,
            )

            # junk из поиска — в rejected для статистики
            for it, reason in junked[:20]:
                from models.product import ProductData as PD

                buckets.rejected.append(
                    (
                        PD(
                            product_id=it.product_id,
                            name=it.name,
                            price=it.price,
                            url=it.url,
                        ),
                        [reason],
                    )
                )

            all_for_ctx = buckets.passed + [p for p, _ in buckets.near_miss]
            prices = [p.price for p in all_for_ctx if p.price > 0] or [
                p.price for p in products if p.price > 0
            ]
            context = SearchContext(
                all_prices=prices,
                median_price=median(prices) if prices else 0.0,
                filters=filters,
            )

            scored = self.scorer.score_all(
                buckets.passed, soft_penalty=False, context=context
            )[: Config.TOP_RESULTS]

            near_products = [p for p, _ in buckets.near_miss][
                : Config.NEAR_MISS_LIMIT
            ]
            near_scored = self.scorer.score_all(
                near_products, soft_penalty=True, context=context
            )

            verdicts: list[str] = []
            if scored:
                try:
                    verdicts = await self.explainer.explain_top3(scored[:3], context)
                except Exception as exc:
                    # Бронь 06.11: сбой ИИ (любой) не отменяет найденные товары
                    logger.warning("explain_stage_failed", error=str(exc))
                    verdicts = []
                for sp, verdict in zip(scored[:3], verdicts):
                    sp.verdict = verdict

            await progress("✅ Готово!")

            response = SearchResponse(
                filters=filters,
                scored=scored,
                rejected=buckets.rejected,
                near_miss=near_scored,
                verdicts=verdicts,
                found_total=len(items) + len(junked),
                query_display=display,
            )
            if not Config.DRY_RUN:
                sqlite_store.cache_set(
                    cache_key, deepcopy(response), Config.CACHE_TTL_SEARCH_SECONDS
                )
            return response
        finally:
            if client is not None:
                await client.aclose()

    async def _search_items(
        self,
        search_text: str,
        filters: SearchFilters,
        client: ComposerClient | None,
        progress: ProgressCallback,
    ) -> list[SearchItem] | None:
        """Firecrawl первым (живой канал). None = лимит исчерпан.

        Composer остаётся аварийным запасом под флагом ENABLE_COMPOSER:
        если Firecrawl пуст, а Composer включён — пробуем его как прежде.
        """
        if Config.ENABLE_FIRECRAWL and not Config.DRY_RUN:
            await progress(
                f"🔍 Ищу на Ozon, Wildberries и Яндекс.Маркете: {search_text}..."
            )
            try:
                items = await channel_search(
                    search_text,
                    limit=Config.SEARCH_ITEMS_LIMIT,
                )
                if items:
                    await progress(f"🔍 Нашёл {len(items)} товаров (поиск по 3 площадкам)")
                    credit_monitor.check_and_alert()
                    return items
            except ProviderQuotaError:
                # оба канала исчерпаны — канал уже разослал алерты
                credit_monitor.check_and_alert()
                return None
            # Firecrawl/Serper пуст — при включённом Composer пробуем его до
            # того, как честно сказать «не нашлось»
        elif Config.DRY_RUN:
            return await parse_search(search_text, client=None)

        if client is not None:
            await progress(f"🔍 Ищу на Ozon: {search_text}...")
            items = await parse_search(
                search_text,
                client=client,
                price_min=filters.price_min,
                price_max=filters.price_max,
            )
            if items:
                return items
        return []

    def _build_search_text(self, filters: SearchFilters) -> str:
        search_text = filters.text
        if (
            "андроид" in search_text
            and (filters.ram_gb or filters.storage_gb)
            and "смартфон" not in search_text
        ):
            search_text = search_text.replace("андроид", "смартфон")
        extras: list[str] = []
        if filters.brand:
            extras.append(filters.brand.lower())
        if filters.ram_gb:
            extras.append(f"{int(filters.ram_gb)}гб")
        if filters.storage_gb:
            extras.append(f"{int(filters.storage_gb)}гб")
        if extras:
            search_text = f"{search_text} {' '.join(extras)}".strip()
        return search_text

    @staticmethod
    def _is_smartphone_query(filters: SearchFilters, search_text: str) -> bool:
        t = f"{filters.text} {search_text}".lower()
        return any(
            w in t
            for w in ("смартфон", "андроид", "телефон", "phone", "iphone")
        ) or bool(filters.ram_gb)

    @staticmethod
    def _cache_key(search_text: str, filters: SearchFilters) -> str:
        payload = {
            "q": search_text,
            "f": filters.__dict__,
        }
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    def pre_rank(self, items: list[SearchItem]) -> list[SearchItem]:
        """Грубый предскор по полям поиска: рейтинг × log(отзывы) / цена."""
        import math

        def key(it: SearchItem) -> float:
            rating = it.rating or 3.5
            reviews = max(it.reviews_count or 0, 1)
            price = max(it.price, 1.0)
            return (rating * math.log10(reviews + 1)) / math.sqrt(price)

        return sorted(items, key=key, reverse=True)

    async def _enrich(
        self,
        items: list[SearchItem],
        client: ComposerClient | None,
    ) -> list[ProductData]:
        products: list[ProductData] = []
        try_detail = (
            (Config.TRY_PRODUCT_DETAIL or Config.USE_BROWSER)
            and client is not None
            and not Config.DRY_RUN
        )
        for idx, item in enumerate(items):
            product = await parse_product(
                item,
                client=client,
                try_detail=try_detail and idx < 3,
            )
            if product:
                products.append(product)
        return products
