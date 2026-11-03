"""Кэш ответов поиска в SQLite: TTL и переживание рестартов."""
from __future__ import annotations

import services.sqlite_store as store


def test_cache_roundtrip(tmp_path):
    db = str(tmp_path / "c.db")
    store.cache_set("k1", {"a": 1}, ttl_seconds=60, path=db)
    assert store.cache_get("k1", path=db) == {"a": 1}


def test_cache_expires(tmp_path):
    db = str(tmp_path / "c.db")
    store.cache_set("k2", [1, 2], ttl_seconds=-1, path=db)
    assert store.cache_get("k2", path=db) is None


def test_cache_survives_reopen(tmp_path):
    db = str(tmp_path / "c.db")
    store.cache_set("k3", "ответ поиска", ttl_seconds=600, path=db)
    assert store.cache_get("k3", path=db) == "ответ поиска"
