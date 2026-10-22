"""Единый экземпляр Jinja2Templates для всех роутеров.

Даёт глобальную функцию content_block(page, key, default) — редактируемые
из админки тексты и надписи кнопок (Этап A1).
"""

from __future__ import annotations

from fastapi.templating import Jinja2Templates

from app import config
from app.content_blocks import get_block

templates = Jinja2Templates(directory=str(config.BASE_DIR / "app" / "templates"))
templates.env.globals["content_block"] = get_block
templates.env.globals["metrika_id"] = config.METRIKA_ID
