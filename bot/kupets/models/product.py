"""Модели данных товаров и продавцов."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SellerData:
    seller_id: str
    seller_name: str
    rating: float | None = None
    reviews_count: int = 0
    positive_percent: float | None = None
    orders_count: int | None = None
    registration_date: str | None = None
    is_verified: bool = False


@dataclass
class ProductSpecs:
    ram_gb: float | None = None
    storage_gb: float | None = None
    screen_inches: float | None = None
    cpu: str | None = None
    color: str | None = None
    battery_mah: int | None = None
    confirmed: bool = False


@dataclass
class SearchItem:
    product_id: str
    name: str
    price: float  # 0.0 = цена из сниппета не извлечена («цена по ссылке»)
    old_price: float | None = None
    rating: float | None = None
    reviews_count: int | None = None
    url: str = ""
    seller_name: str | None = None
    seller_id: str | None = None
    has_fbo: bool = False
    delivery_text: str | None = None
    marketplace: str = "ozon"  # ozon | wb | yam


@dataclass
class ProductData:
    product_id: str
    name: str
    price: float
    url: str
    description: str | None = None
    old_price: float | None = None
    rating: float | None = None
    reviews_count: int = 0
    rating_distribution: dict[int, int] | None = None
    seller_name: str = ""
    seller_id: str = ""
    seller_url: str = ""
    seller: SellerData | None = None
    delivery_type: str = "FBS"
    delivery_days: int | None = None
    delivery_price: float = 0.0
    options: list[str] = field(default_factory=list)
    category: str | None = None
    photos: list[str] = field(default_factory=list)
    ram_gb: float | None = None
    storage_gb: float | None = None
    screen_inches: float | None = None
    cpu: str | None = None
    color: str | None = None
    battery_mah: int | None = None
    specs_confirmed: bool = False
    rejection_reasons: list[str] = field(default_factory=list)
    near_miss_reasons: list[str] = field(default_factory=list)
    marketplace: str = "ozon"  # ozon | wb | yam
