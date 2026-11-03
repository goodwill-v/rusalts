"""Smoke-тест пайплайна в DRY_RUN."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["DRY_RUN"] = "true"

# перезагрузка конфига после env
import importlib
import config as config_mod

importlib.reload(config_mod)

from services.search_pipeline import SearchPipeline  # noqa: E402
from bot.messages import format_results  # noqa: E402


async def main() -> None:
    pipeline = SearchPipeline()
    query = (
        "найди андроид с оперативкой от 8ГБ, встроенной памятью от 128 ГБ "
        "и экраном от 5,5 дюймов до 20000 руб"
    )
    response = await pipeline.run(query)
    text = format_results(response)
    print(text)
    print("---")
    print(
        f"passed={len(response.scored)} "
        f"rejected={len(response.rejected)} "
        f"near_miss={len(response.near_miss)}"
    )
    assert response.found_total > 0
    assert response.scored, "ожидался хотя бы 1 прошедший товар"
    assert response.scored[0].score > 0


if __name__ == "__main__":
    asyncio.run(main())
