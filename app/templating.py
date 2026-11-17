"""Единый экземпляр Jinja2Templates для всех роутеров.

Даёт глобальную функцию content_block(page, key, default) — редактируемые
из админки тексты и надписи кнопок (Этап A1).
"""

from __future__ import annotations

from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from app import config
from app.content_blocks import get_block as _get_block

templates = Jinja2Templates(directory=str(config.BASE_DIR / "app" / "templates"))


def get_block(page: str, key: str, default: str = "") -> Markup:
    """Контент-блок из админки как HTML (решение Шефа 10.11: теги и
    переносы Enter работают во всех текстовых полях). Контент редактируется
    только через админку за Basic Auth — доверенный источник."""
    return Markup(_get_block(page, key, default))


templates.env.globals["content_block"] = get_block
templates.env.globals["metrika_id"] = config.METRIKA_ID
