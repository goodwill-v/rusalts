"""In-memory кэш с TTL. Интерфейс совместим с будущим Redis."""

from __future__ import annotations

import time
from typing import Any, Generic, TypeVar

from config import Config

T = TypeVar("T")


class MemoryCache(Generic[T]):
    def __init__(self, ttl_seconds: int | None = None) -> None:
        self._ttl = ttl_seconds if ttl_seconds is not None else Config.CACHE_TTL_SECONDS
        self._store: dict[str, tuple[float, T]] = {}

    def get(self, key: str) -> T | None:
        item = self._store.get(key)
        if item is None:
            return None
        expires_at, value = item
        if time.monotonic() > expires_at:
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: str, value: T, ttl: int | None = None) -> None:
        lifetime = ttl if ttl is not None else self._ttl
        self._store[key] = (time.monotonic() + lifetime, value)

    def clear(self) -> None:
        self._store.clear()


# Глобальный кэш продавцов
seller_cache: MemoryCache[Any] = MemoryCache()
