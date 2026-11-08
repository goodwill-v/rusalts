"""Юнит-тесты zcode_rpc: приоритет ingress-токена и сериализация протокола."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.zcode_rpc import (  # noqa: E402
    _deserialize,
    _read_ingress_token,
    _serialize,
)


def test_token_env_used_when_no_files(monkeypatch, tmp_path):
    monkeypatch.setenv("ZCODE_INGRESS_TOKEN", "env-token")
    monkeypatch.delenv("ZCODE_INGRESS_TOKEN_FILE", raising=False)
    monkeypatch.setattr("app.zcode_rpc._DEFAULT_TOKEN_PATHS", (str(tmp_path / "missing"),))
    assert _read_ingress_token() == "env-token"


def test_token_live_file_wins_over_static_env(monkeypatch, tmp_path):
    # Файлы обновляются живо (монтаж секрета / systemd-синхронизация), env —
    # статичная копия, которая устарает после рестарта zcode-server.
    live = tmp_path / "live-token"
    live.write_text("fresh-token\n")
    monkeypatch.setenv("ZCODE_INGRESS_TOKEN", "stale-token")
    monkeypatch.delenv("ZCODE_INGRESS_TOKEN_FILE", raising=False)
    monkeypatch.setattr("app.zcode_rpc._DEFAULT_TOKEN_PATHS", (str(live),))
    assert _read_ingress_token() == "fresh-token"


def test_token_explicit_file_env_has_top_priority(monkeypatch, tmp_path):
    explicit = tmp_path / "explicit-token"
    explicit.write_text("explicit-token")
    default = tmp_path / "default-token"
    default.write_text("default-token")
    monkeypatch.setenv("ZCODE_INGRESS_TOKEN_FILE", str(explicit))
    monkeypatch.setattr("app.zcode_rpc._DEFAULT_TOKEN_PATHS", (str(default),))
    assert _read_ingress_token() == "explicit-token"


def test_token_reconnect_rereads_file(monkeypatch, tmp_path):
    # При рестарте zcode-server файл перезаписывается; connect() перечитывает
    # его при каждом подключении — проверяем, что чтение не кэшируется.
    live = tmp_path / "live-token"
    live.write_text("token-1")
    monkeypatch.delenv("ZCODE_INGRESS_TOKEN", raising=False)
    monkeypatch.delenv("ZCODE_INGRESS_TOKEN_FILE", raising=False)
    monkeypatch.setattr("app.zcode_rpc._DEFAULT_TOKEN_PATHS", (str(live),))
    assert _read_ingress_token() == "token-1"
    live.write_text("token-2")
    assert _read_ingress_token() == "token-2"


def test_serialize_roundtrip():
    value = ["zcode-agent", 42, None, True, {"sessionId": "sess_1", "limit": 10}]
    restored, _ = _deserialize(_serialize(value), 0)
    assert restored == value


def test_vlq_long_lengths():
    # Длина строки > 16384 — многобайтовый VLQ (реальный кейс: длинные ответы)
    text = "x" * 100_000
    restored, _ = _deserialize(_serialize(text), 0)
    assert restored == text
