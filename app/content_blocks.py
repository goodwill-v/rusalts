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
    "laws": [
        ("h1", "Заголовок H1", "input", "Правовая база"),
        ("lead", "Вводный тезис (под H1)", "textarea",
         "Соответствие 152-ФЗ, 168-ФЗ и 149-ФЗ — без штрафов и блокировок. "
         "Разбираем требования к сайтам, интерфейсам и работе с данными."),
        ("law152_title", "152-ФЗ — заголовок", "input", "152-ФЗ «О персональных данных»"),
        ("law152_text", "152-ФЗ — текст", "textarea",
         "Требования к сбору, хранению и обработке ПДн; права субъекта; "
         "уведомление РКН; штрафы за нарушения."),
        ("law152_link", "152-ФЗ — ссылка «подробнее»", "input", "/legal/privacy/"),
        ("law168_title", "168-ФЗ — заголовок", "input", "168-ФЗ о государственном языке"),
        ("law168_text", "168-ФЗ — текст", "textarea",
         "Требования к русскому языку в интерфейсах, наименованиях и рекламе."),
        ("law149_title", "149-ФЗ — заголовок", "input", "149-ФЗ «Об информации…»"),
        ("law149_text", "149-ФЗ — текст", "textarea",
         "Базовые правила распространения информации, доступа к сервисам и ответственности."),
        ("rkn_title", "РКН — заголовок", "input", "Роскомнадзор: уведомления и проверки"),
        ("rkn_text", "РКН — текст", "textarea",
         "Уведомление о начале обработки ПДн, готовность к проверке, реестры и блокировки."),
        ("ai_title", "ИИ-регулирование — заголовок", "input", "Регулирование ИИ и цифровых сервисов"),
        ("ai_text", "ИИ-регулирование — текст", "textarea",
         "Обзор требований к ИИ-сервисам: что уже действует и к чему готовиться."),
        ("topics_title", "«Что мы разбираем» — заголовок", "input", "Что мы разбираем"),
        ("topics_text", "«Что мы разбираем» — список (HTML)", "textarea",
         "<ul><li>Какие данные собирает ваш сайт и что об этом должно быть написано</li>"
         "<li>Уведомление в РКН — пошагово</li>"
         "<li>Cookie-баннер: когда обязателен</li>"
         "<li>Русский язык в интерфейсе: что проверяет надзор</li>"
         "<li>Готовность ИИ-сервиса к новым требованиям</li></ul>"),
        ("samples_title", "«Образцы документов» — заголовок", "input", "Образцы документов"),
        ("samples_text", "Образцы — текст + ссылки (HTML)", "textarea",
         "Шаблоны (на юрпроверку) и наши готовые страницы: "
         "<a href='/legal/privacy/'>Политика ПДн</a>, "
         "<a href='/legal/cookies/'>Cookies</a>, "
         "<a href='/legal/consent/'>Согласие на обработку</a>, "
         "<a href='/legal/subject-request/'>Запрос субъекта</a>. "
         "Критические документы — только ссылками на официальные источники."),
        ("sources_title", "«Официальные источники» — заголовок", "input", "Официальные источники"),
        ("sources_text", "Источники — ссылки (HTML)", "textarea",
         "<a href='http://pravo.gov.ru/' target='_blank' rel='noopener'>pravo.gov.ru</a> — "
         "официальный интернет-портал правовой информации; портал и реестры Роскомнадзора."),
        ("cta_title", "CTA — заголовок", "input", "Проверить мой сайт на соответствие"),
        ("cta_text", "CTA — текст", "textarea",
         "Разберём, что нужно исправить именно на вашем сайте."),
        ("cta_btn_text", "CTA — текст кнопки", "input", "Заказать правовой аудит"),
        ("cta_btn_url", "CTA — ссылка кнопки", "input", "/request/"),
    ],
    "channels": [
        ("h1", "Заголовок H1", "input", "Каналы"),
        ("lead", "Вводная строка", "textarea",
         "Здесь мы живём и разговариваем. Заходите — будет понятно и по-домашнему."),
        ("dzen_title", "Дзен — название", "input", "Дзен"),
        ("dzen_desc", "Дзен — описание", "input", "Лонгриды и разборы"),
        ("dzen_url", "Дзен — ссылка", "input", ""),
        ("vk_title", "ВК — название", "input", "ВКонтакте «Альтернатива»"),
        ("vk_desc", "ВК — описание", "input", "Сообщество, анонсы, обсуждения"),
        ("vk_url", "ВК — ссылка", "input", "https://vk.com/alternat777"),
        ("max_title", "МАКС — название", "input", "МАКС-канал «AI для Дела»"),
        ("max_desc", "МАКС — описание", "input", "Кейсы и демо ботов"),
        ("max_url", "МАКС — ссылка", "input",
         "https://max.ru/u/f9LHodD0cOKMhFRsnfRVOT-ChF-0t--tFtjM0b0MwNMgVZQmvpzPQave_6Q"),
        ("vc_title", "VC.ru — название", "input", "VC.ru"),
        ("vc_desc", "VC.ru — описание", "input", "Деловые версии материалов"),
        ("vc_url", "VC.ru — ссылка", "input", ""),
        ("pikabu_title", "Пикабу — название", "input", "Пикабу"),
        ("pikabu_desc", "Пикабу — описание", "input", "Лёгкие версии, с юмором"),
        ("pikabu_url", "Пикабу — ссылка", "input", ""),
        ("wikiclaw_title", "WikiClaw — название", "input", "WikiClaw"),
        ("wikiclaw_desc", "WikiClaw — описание", "input", "Образование, техническая аудитория"),
        ("wikiclaw_url", "WikiClaw — ссылка", "input", ""),
        ("posts_title", "«Свежие публикации» — заголовок", "input", "Свежие публикации"),
        ("posts_text", "Свежие публикации — список (HTML)", "textarea",
         "<ul><li>Предисловие Шефа — как мы решили строить систему</li>"
         "<li>Очерк Алик — «Как мы строили систему»</li>"
         "<li>Очерк Веж — «Как я приручил бота-сторожа»</li>"
         "<li>Очерк Куп — «Как я научил робота не верить скидкам»</li>"
         "<li>Очерк Админ — «Как я навёл порядок в витрине»</li>"
         "<li>Очерк Кор — «Как я воспитывал команду агентов»</li></ul>"),
        ("cd_title", "Новосёлы ЦД — заголовок", "input", "Мастерская цифрового уюта: Новосёлы ЦД"),
        ("cd_text", "Новосёлы ЦД — текст", "textarea",
         "Цифровой дом: азбука для новосёлов. Дом в три этажа — устройство, сети, нейросети. "
         "Занятие — 20–25 минут, одна чашка чая: загадка, метафора, история, практика, вывод."),
        ("cd_btn_text", "Новосёлы ЦД — кнопка", "input", "Заглянуть в мастерскую"),
        ("cd_btn_url", "Новосёлы ЦД — ссылка кнопки", "input", ""),
        ("what_title", "«Что мы делаем с каналами» — заголовок", "input", "Что мы делаем с каналами"),
        ("what_text", "Что делаем — текст", "textarea",
         "Настроим бот и канал под ключ: Telegram/ВК/МАКС, миграция аудитории, сценарии. "
         "Кейсы: ВежПом, Купец."),
        ("cta_title", "CTA — заголовок", "input", "Нужен бот в вашем канале?"),
        ("cta_text", "CTA — текст", "textarea",
         "Расскажем, как перенести общение с клиентами в удобный канал."),
        ("cta_btn_text", "CTA — кнопка", "input", "Оставить заявку"),
        ("cta_btn_url", "CTA — ссылка", "input", "/request/"),
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
    "laws": "Правовая база /laws/",
    "channels": "Каналы /channels/",
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
