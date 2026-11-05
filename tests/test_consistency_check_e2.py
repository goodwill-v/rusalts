"""E2 ТЗ Развития: ночной чек согласованности (модуль consistency_check).

HTTP вынесен в fetch-колбэк — в тестах подставляем TestClient без сети.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config


@pytest.fixture()
def client(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "http://testserver")
    from app.main import app

    from app.tariffs_store import seed_defined_products
    seed_defined_products()
    return TestClient(app)


def _tc_fetch(client):
    async def fetch(url: str):
        r = client.get(url)
        return r.status_code, r.text

    return fetch


async def _broken_fetch(url: str):
    if url.endswith("/news/"):
        return 503, "down"
    r = {"price": "999 ₽"}  # не текст страницы — цены планов «не найдутся»
    return 200, "<html></html>"


def test_check_green_on_healthy_site(client) -> None:
    from app.consistency_check import format_alert, run_consistency_check

    report = _run(client)
    assert report["ok"], report["issues"]
    assert report["checked"] >= 10
    assert "🟢" in format_alert(report)


def _run(client):
    import asyncio

    from app.consistency_check import run_consistency_check

    return asyncio.run(run_consistency_check(fetch=_tc_fetch(client)))


def test_check_catches_breakage(client) -> None:
    import asyncio

    from app.consistency_check import format_alert, run_consistency_check

    async def broken_fetch(url: str):
        if url.endswith("/news/"):
            return 503, "down"
        if url.endswith("/techologis/p/kupec/"):
            return 200, "<html>планы пропали</html>"
        return (await _tc_fetch(client)(url))

    report = asyncio.run(run_consistency_check(fetch=broken_fetch))
    assert not report["ok"]
    assert any("/news/" in i for i in report["issues"])
    assert any("kupec" in i and "не на странице" in i for i in report["issues"])
    assert "🔴" in format_alert(report)
