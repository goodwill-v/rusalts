"""Живой smoke-тест поиска Ozon (DRY_RUN=false)."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["DRY_RUN"] = "false"
os.environ["PARSING_DELAY_MS"] = "400"

import importlib
import config as config_mod

importlib.reload(config_mod)

from bot.messages import format_results
from services.search_pipeline import SearchPipeline


async def main() -> None:
    pipeline = SearchPipeline()
    query = "найди андроид с оперативкой от 8ГБ, до 20000 руб"
    response = await pipeline.run(query)
    print(format_results(response))
    print("---")
    print(
        f"found={response.found_total} passed={len(response.scored)} "
        f"rejected={len(response.rejected)} near={len(response.near_miss)}"
    )
    if response.scored:
        print("top:", response.scored[0].product.name, response.scored[0].score)
        print("url:", response.scored[0].product.url)
    assert response.found_total > 0, "поиск не вернул товары"
    assert response.scored or response.near_miss, "всё отсеялось без near_miss"


if __name__ == "__main__":
    asyncio.run(main())
