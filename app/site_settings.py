"""Настройки сайта (JSON в data/content/site_settings.json).

Хранит тумблер автоматической публикации новостей и редактируемые поля
главной страницы (герой). Отсутствующие ключи → значения по умолчанию.
"""

from __future__ import annotations

import json
import threading
from typing import Any

from app import config

_PATH = config.DATA_DIR / "content" / "site_settings.json"
_LOCK = threading.Lock()

DEFAULTS: dict[str, Any] = {
    "auto_publish": False,
    "hero_title": "АЛЬТЕРНАТИВА — ваш юридически безупречный переход в российский цифровой суверенитет",
    "hero_subtitle": "Помогаем бизнесу, блогерам, сообществам. Разработка ИИ‑приложений и консультации по отечественным платформам",
    "hero_cta_text": "Пройти бесплатную диагностику",
    "hero_cta_url": "/diagnostics/",
}

AUTO_PUBLISH_KEY = "auto_publish"


def _read_raw() -> dict[str, Any]:
    try:
        with open(_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def get_all() -> dict[str, Any]:
    data = dict(DEFAULTS)
    data.update(_read_raw())
    return data


def update(fields: dict[str, Any]) -> dict[str, Any]:
    allowed = set(DEFAULTS) - {"auto_publish"}
    with _LOCK:
        current = _read_raw()
        for k in allowed:
            if k in fields and fields[k] is not None:
                current[k] = str(fields[k]).strip() or DEFAULTS[k]
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_PATH, "w", encoding="utf-8") as f:
            json.dump(current, f, ensure_ascii=False, indent=2)
    return get_all()


def get_auto_publish() -> bool:
    return bool(_read_raw().get(AUTO_PUBLISH_KEY, False))


def set_auto_publish(value: bool) -> bool:
    with _LOCK:
        current = _read_raw()
        current[AUTO_PUBLISH_KEY] = bool(value)
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_PATH, "w", encoding="utf-8") as f:
            json.dump(current, f, ensure_ascii=False, indent=2)
    return bool(value)
