"""Контент-блоки сайта (Этап A1 ТЗ Развития): редактируемые из админки тексты и надписи кнопок.

Хранение: SQLite data/content_blocks.db, таблица site_content (page, key, value).
Фолбэк: page='index' hero-ключи → site_settings.json (старый таб «Главная»), затем — дефолт из шаблона.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

from app import config

DB_PATH = config.DATA_DIR / "content_blocks.db"
_LOCK = threading.Lock()

# Схема редактируемых блоков: страница → [(ключ, подпись, тип, дефолт)]
SCHEMA: dict[str, list[tuple[str, str, str, str]]] = {
    "base": [
        ("header_cta_text", "Кнопка в шапке — текст", "input", "Бесплатная диагностика"),
        ("header_cta_url", "Кнопка в шапке — ссылка", "input", "/diagnostics/"),
        ("footer_about", "Футер — о компании (кратко)", "textarea",
         "АЛТ — Альтернативные Легальные Технологии"),
    ],
    "index": [
        ("hero_title", "Герой — заголовок (H1)", "textarea",
         "АЛЬТЕРНАТИВА — ваш юридически безупречный переход в российский цифровой суверенитет"),
        ("hero_subtitle", "Герой — подзаголовок", "textarea",
         "Помогаем бизнесу, блогерам, сообществам. Разработка ИИ‑приложений и консультации по отечественным платформам"),
        ("hero_cta_text", "Герой — текст CTA-кнопки", "input", "Пройти бесплатную диагностику"),
        ("hero_cta_url", "Герой — ссылка CTA-кнопки", "input", "/diagnostics/"),
    ],
    "diagnostics": [
        ("h1", "Заголовок страницы (H1)", "textarea",
         "Сократи свой рабочий день с ИИ — начни с диагностики"),
        ("btn_free", "Кнопка «Бесплатная диагностика» — текст", "input",
         "Пройти бесплатную диагностику →"),
        ("btn_your_business", "Кнопка «Ваше дело» — текст", "input", "Ваше дело →"),
        ("btn_paid", "Кнопка платной диагностики — текст", "input",
         "Получить стратегию автономизации — 3 000 ₽ →"),
    ],
    "showcase": [
        ("title", "Витрина — заголовок", "input", "Продукты и услуги АЛТ"),
        ("lead", "Витрина — вводный текст", "textarea",
         "Представленные на сайте ИИ-сервисы позволяют решать разнообразные задачи. "
         "Спектр возможностей инструментов с искусственным интеллектом, который создаём мы, "
         "намного шире представленных здесь примеров. Мы поможем вам сформулировать задачи, "
         "исходя из ваших процессов, и создадим те инструменты, которые помогут в решении именно ваших задач."),
    ],
}

PAGE_TITLES = {
    "base": "Шапка и футер (все страницы)",
    "index": "Главная страница",
    "diagnostics": "Диагностика",
    "showcase": "Витрина /techologis/",
}


def _connect() -> sqlite3.Connection:
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute(
        """CREATE TABLE IF NOT EXISTS site_content (
            page TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (page, key)
        )"""
    )
    return db


def _read_all(page: str) -> dict[str, str]:
    with closing(_connect()) as db:
        rows = db.execute(
            "SELECT key, value FROM site_content WHERE page = ?", (page,)
        ).fetchall()
    return {r["key"]: r["value"] for r in rows if r["value"] != ""}


def get_block(page: str, key: str, default: str = "") -> str:
    """Цепочка: БД → site_settings (hero) → дефолт шаблона."""
    stored = _read_all(page).get(key)
    if stored:
        return stored
    if page == "index":
        try:
            from app import site_settings

            val = site_settings.get_all().get(key, "")
            if val:
                return str(val)
        except Exception:
            pass
    return default


def blocks_for(page: str) -> dict[str, str]:
    """Дефолты схемы, переопределённые сохранёнными значениями (для UI и маршрутов)."""
    out = {key: default for key, _, _, default in SCHEMA.get(page, [])}
    out.update(_read_all(page))
    if page == "index":
        try:
            from app import site_settings

            settings = site_settings.get_all()
            for key in out:
                if key in settings and settings[key]:
                    out[key] = str(settings[key])
        except Exception:
            pass
    return out


def set_blocks(page: str, values: dict[str, Any]) -> None:
    if page not in SCHEMA:
        raise ValueError(f"неизвестная страница: {page}")
    allowed = {key for key, _, _, _ in SCHEMA[page]}
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    with _LOCK, closing(_connect()) as db:
        for key, value in values.items():
            if key not in allowed:
                continue
            db.execute(
                "INSERT INTO site_content (page, key, value, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(page, key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (page, key, str(value or "").strip(), now),
            )
        db.commit()
