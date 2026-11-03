"""CI-безопасность Купца: тяжёлые тесты — только там, где есть зависимости.

В CI ставятся только requirements(-dev) монорепо: там есть structlog и
python-dotenv (их хватает тестам на чистую логику — адаптер, guard, кредиты,
кэш), но нет aiogram/curl_cffi/openai/bs4 — файлы, которые их импортируют,
исключаются из сбора.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_HEAVY = ("aiogram", "curl_cffi", "openai", "bs4")
_HEAVY_FILES = [
    "test_pipeline_dry_run.py",   # SearchPipeline → ai.explainer → openai
    "test_vision.py",             # vision.recognizer → openai
    "test_live_search.py",        # живой smoke: SearchPipeline + сеть
]

_missing = [name for name in _HEAVY if importlib.util.find_spec(name) is None]

collect_ignore = _HEAVY_FILES if _missing else []
