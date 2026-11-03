"""Оркестратор поиска: фильтры → парсинг → SCORE → AI."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from copy import deepcopy

from ai.explainer import AIExplainer
from cache.memory_cache import MemoryCache
from config import Config
from engine.filter import apply_filters
from engine.junk import filter_junk_items
from engine.normalizer import median
from engine.query_parser import extract_filters
from engine.scorer import ProductScorer
from models.product import ProductData, SearchItem
from models.result import SearchContext, SearchFilters, SearchResponse
from parser.circuit import ozon_circuit
from parser.composer import ComposerClient
from parser.firecrawl_search import FirecrawlClient
from parser.product import parse_product
from parser.search import parse_search
from parser.seller import attach_sellers
from utils import get_logger

logger = get_logger("services.pipeline")

ProgressCallback = Callable[[str], Awaitable[None]]

# Кэш полных ответов поиска (5–10 мин по CACHE_TTL)
_response_cache: MemoryCache[SearchResponse] = MemoryCache()


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

        # Если есть Firecrawl — не блокируемся на circuit, пробуем Composer → fallback
        if ozon_circuit.is_open and not Config.DRY_RUN and not Config.ENABLE_FIRECRAWL:
            wait = ozon_circuit.remaining_sec()
            filters = filters_override or extract_filters(query)
            display = (
                filters_override.text if filters_override else query
            )
            await progress(
                f"⚠️ Ozon временно ограничивает запросы. "
                f"Подожди ~{wait} сек и попробуй снова."
            )
            return SearchResponse.blocked(
                filters,
                query_display=display,
                retry_sec=wait,
            )

        filters = filters_override or extract_filters(query)
        display = filters.text or query
        search_text = self._build_search_text(filters)
        smartphone_query = self._is_smartphone_query(filters, search_text)

        cache_key = self._cache_key(search_text, filters)
        if not skip_cache and not Config.DRY_RUN:
            cached = _response_cache.get(cache_key)
            if cached is not None:
                logger.info("cache_hit", key=cache_key[:16])
                await progress("✅ Из кэша")
                return deepcopy(cached)

        logger.info(
            "pipeline_start",
            query=query,
            search_text=search_text,
            filters=filters.__dict__,
        )

        await progress(f"🔍 Ищу на Ozon: {display}...")

        client: ComposerClient | None = None
        if not Config.DRY_RUN:
            client = ComposerClient()

        try:
            items = await parse_search(
                search_text,
                client=client,
                price_min=filters.price_min,
                price_max=filters.price_max,
            )
            # Fallback to Firecrawl if Composer API is blocked
            if not items and (ozon_circuit.is_open or (client and client.last_blocked)) and Config.ENABLE_FIRECRAWL:
                logger.info("firecrawl_fallback", query=search_text)
                fc = FirecrawlClient()
                try:
                    items = await fc.search_ozon(
                        search_text,
                        price_min=filters.price_min,
                        price_max=filters.price_max,
                        limit=Config.SEARCH_ITEMS_LIMIT,
                    )
                    if items:
                        ozon_circuit.record_success()
                        await progress(f"🔍 Нашёл {len(items)} товаров (Firecrawl)")
                finally:
                    await fc.aclose()
            if not items:
                fc_tried = Config.ENABLE_FIRECRAWL and not Config.DRY_RUN
                if not fc_tried and (client and client.last_blocked):
                    wait = ozon_circuit.remaining_sec()
                    logger.warning(
                        "search_blocked_by_ozon",
                        query=search_text,
                        retry_sec=wait,
                    )
                    await progress(
                        "⚠️ Ozon заблокировал запрос (антибот)."
                    )
                    return SearchResponse.blocked(
                        filters,
                        query_display=display,
                        retry_sec=wait,
                    )
                await progress("😕 Ничего не нашёл")
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
            prices = [p.price for p in all_for_ctx] or [p.price for p in products]
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
                verdicts = await self.explainer.explain_top3(scored[:3], context)
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
                _response_cache.set(cache_key, deepcopy(response))
            return response
        finally:
            if client is not None:
                await client.aclose()

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
