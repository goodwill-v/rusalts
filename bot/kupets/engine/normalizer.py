"""Min-max нормализация."""

from __future__ import annotations

import math


def normalize_min_max(values: list[float]) -> list[float]:
    """Min-max нормализация к [0, 1]."""
    if not values:
        return []
    mn, mx = min(values), max(values)
    if mx == mn:
        return [0.5] * len(values)
    return [(v - mn) / (mx - mn) for v in values]


def log_scale(value: float, cap: float = 4.0) -> float:
    """Логарифмическая шкала для числа отзывов → [0, 1]."""
    if value <= 0:
        return 0.0
    return min(math.log10(value) / cap, 1.0)


def median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0
