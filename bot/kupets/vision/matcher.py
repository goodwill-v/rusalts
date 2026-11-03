"""Заготовка матчера изображений — v1.1."""

from __future__ import annotations


def cosine_similarity(a: list[float], b: list[float]) -> float:
    raise NotImplementedError("Vision matcher — v1.1")


def stock_photo_risk(photo_urls: list[str], threshold_sellers: int = 5) -> bool:
    """True, если одно фото встречается у многих продавцов."""
    raise NotImplementedError("Stock photo risk — v1.1")
