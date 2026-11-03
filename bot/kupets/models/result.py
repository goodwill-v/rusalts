"""Результаты поиска и скоринга."""

from __future__ import annotations

from dataclasses import dataclass, field

from models.product import ProductData


@dataclass
class SearchFilters:
    text: str
    price_max: float | None = None
    price_min: float | None = None
    ram_gb: float | None = None
    storage_gb: float | None = None
    screen_inches: float | None = None
    brand: str | None = None
    only_fbo: bool = False


@dataclass
class ScoreBreakdown:
    seller: float = 0.0
    price: float = 0.0
    reviews: float = 0.0
    delivery: float = 0.0
    soft_penalty: float = 0.0

    @property
    def total_points(self) -> float:
        return (
            self.seller
            + self.price
            + self.reviews
            + self.delivery
            - self.soft_penalty
        )


@dataclass
class ScoredProduct:
    product: ProductData
    score: int
    breakdown: ScoreBreakdown
    verdict: str = ""
    match_notes: list[str] = field(default_factory=list)


@dataclass
class SearchContext:
    all_prices: list[float]
    median_price: float
    filters: SearchFilters


@dataclass
class SearchResponse:
    filters: SearchFilters
    scored: list[ScoredProduct] = field(default_factory=list)
    rejected: list[tuple[ProductData, list[str]]] = field(default_factory=list)
    near_miss: list[ScoredProduct] = field(default_factory=list)
    verdicts: list[str] = field(default_factory=list)
    found_total: int = 0
    query_display: str = ""
    # True, если поиск не удался из‑за антибота Ozon (403/429/circuit)
    ozon_unavailable: bool = False
    ozon_retry_sec: int = 0
    # True, если поиск остановлен исчерпанием лимитов поискового провайдера
    provider_limited: bool = False

    @classmethod
    def empty(cls, filters: SearchFilters) -> SearchResponse:
        return cls(filters=filters, found_total=0, query_display=filters.text)

    @classmethod
    def blocked(
        cls,
        filters: SearchFilters,
        *,
        query_display: str = "",
        retry_sec: int = 0,
    ) -> SearchResponse:
        return cls(
            filters=filters,
            found_total=0,
            query_display=query_display or filters.text,
            ozon_unavailable=True,
            ozon_retry_sec=max(0, retry_sec),
        )
