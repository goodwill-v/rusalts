"""ЮKassa payment integration for the Techologis showcase.
Supports sandbox (demo) mode and live mode with redirect confirmation."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from app import config

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

PRODUCTS: dict[str, dict[str, Any]] = {
    "vezhpom": {
        "id": "vezhpom",
        "name": "ВежПом — мониторинг серверов",
        "description": "Контроль доступности сайтов и серверов (HTTP/HTTPS, ping, порты). Уведомления о сбоях и восстановлении в чат MAX.",
        "price_rub": 150,
        "period_days": 30,
        "trial_days": 14,
        "category": "product",
        "platform": "MAX (бот)",
        "bot_url": "https://web.max.ru/id667207863875_1_bot",
        "status": "available",
    },
    "neyrosotrudnik": {
        "id": "neyrosotrudnik",
        "name": "НейроСотрудник — ИИ-ассистент",
        "description": "ИИ-ассистент на GigaChat (Сбер) с индивидуальными настройками. Отвечает на вопросы, помогает с текстами и рутиной.",
        "price_rub": 500,  # цена от Шефа (Витрина.xlsx)
        "period_days": 30,
        "trial_days": 14,
        "category": "product",
        "platform": "MAX (бот)",
        "bot_url": "https://web.max.ru/428555001",
        "status": "coming",
    },
    "alt_expert": {
        "id": "alt_expert",
        "name": "АЛТ-эксперт — консультант",
        "description": "ИИ-консультант по правовым и технологическим вопросам: 152‑ФЗ, 168‑ФЗ, миграция на отечественные платформы.",
        "price_rub": 0,
        "period_days": 0,
        "trial_days": 0,
        "category": "product",
        "platform": "ВК + Сайт",
        "bot_url": "/consultant",
        "status": "free",
    },
    "italidia": {
        "id": "italidia",
        "name": "ItaLidia — ассистент итальянского языка",
        "description": "ИИ-помощник для изучения итальянского языка. Для студентов, преподавателей, учебных чатов.",
        "price_rub": 300,  # цена от Шефа (Витрина.xlsx)
        "period_days": 30,
        "trial_days": 7,
        "category": "product",
        "platform": "Telegram / MAX",
        "bot_url": "https://t.me/italecho_bot",
        "detail_url": "/techologis/italidia/",
        "status": "available",
    },
    "italidia_test": {
        "id": "italidia_test",
        "name": "🧪 ItaLidia — тест",
        "description": "ТЕСТОВЫЙ режим: подписка на 4 дня, 120K токенов. Для проверки оплаты и цикла.",
        "price_rub": 300,
        "period_days": 4,
        "trial_days": 1,
        "category": "product",
        "platform": "🧪 Тестовый",
        "bot_url": "https://t.me/italecho_bot",
        "status": "available",
    },
    "kupec": {
        "id": "kupec",
        "name": "Купец — анализ маркетплейсов",
        "description": "ИИ-поиск и анализ товаров Ozon: находит по описанию, сравнивает цены, готовит сводки для продавцов.\nБесплатно (до решения проблемы парсинга).",
        "price_rub": 0,
        "period_days": 0,
        "trial_days": 0,
        "category": "product",
        "platform": "MAX (бот)",
        "bot_url": "",
        "status": "free",  # бесплатно до решения проблемы парсинга
    },
    "openclaw": {
        "id": "openclaw",
        "name": "OpenClaw — универсальный ассистент",
        "description": "Установка, настройка, сопровождение OpenClaw на отдельном VPS с индивидуальными настройками. Telegram, MAX, web-интерфейс, терминал.\nУстановка: 5000₽ + Настройка VPS: 3000₽ + Консультации (1 мес): 5000₽.",
        "price_rub": 5000,
        "period_days": 0,
        "trial_days": 0,
        "category": "service",
        "platform": "VPS / под ключ",
        "bot_url": "",
        "status": "inquiry",  # Установка: 5000 + Настройка VPS: 3000 + Консультации 1мес: 5000
    },
    "ozhivlenie": {
        "id": "ozhivlenie",
        "name": "Оживление фото → видео",
        "description": "Превращение статичных фотографий в короткие видео с помощью ИИ.",
        "price_rub": 1000,
        "period_days": 0,
        "trial_days": 0,
        "category": "service",
        "platform": "Услуга",
        "bot_url": "",
        "status": "inquiry",  # От 1000₽/шт
    },
    "tolko": {
        "id": "tolko",
        "name": "ТОЛК — веб-панель управления",
        "description": "Веб-панель для общения и управления всеми ИИ-ассистентами АЛТ в одном интерфейсе.",
        "price_rub": 0,
        "period_days": 0,
        "trial_days": 0,
        "category": "product",
        "platform": "Веб",
        "bot_url": "",
        "status": "dev",  # По договоренности, исходя из функциональных запросов
    },
}

# ---------------------------------------------------------------------------
# Database (SQLite — local payments & subscriptions ledger)
# ---------------------------------------------------------------------------

_DB: sqlite3.Connection | None = None


def _connect() -> sqlite3.Connection:
    """Отдельное соединение SQLite на операцию.

    Кэш одного соединения на модуль падал с sqlite3.ProgrammingError
    ("SQLite objects created in a thread can only be used in that same thread"),
    когда запросы обрабатывались в разных потоках пула uvicorn.
    """
    db = sqlite3.connect(str(config.DATA_DIR / "payments.db"))
    db.row_factory = sqlite3.Row
    db.execute(
        """CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            yookassa_id TEXT UNIQUE,
            product_id TEXT NOT NULL,
            amount_rub INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            customer_email TEXT,
            customer_phone TEXT,
            customer_id TEXT,
            payment_method_id TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payment_id INTEGER NOT NULL,
            product_id TEXT NOT NULL,
            customer_email TEXT,
            payment_method_id TEXT,
            customer_id TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            started_at TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at TEXT,
            FOREIGN KEY(payment_id) REFERENCES payments(id)
        )"""
    )
    return db


def record_payment(
    yookassa_id: str,
    product_id: str,
    amount_rub: int,
    status: str = "pending",
    customer_email: str = "",
    customer_phone: str = "",
    customer_id: str = "",
    payment_method_id: str = "",
) -> int:
    with closing(_connect()) as db:
        cur = db.execute(
            """INSERT INTO payments (yookassa_id, product_id, amount_rub, status, customer_email, customer_phone, customer_id, payment_method_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (yookassa_id, product_id, amount_rub, status, customer_email, customer_phone, customer_id, payment_method_id),
        )
        db.commit()
        return cur.lastrowid


def update_payment_status(yookassa_id: str, status: str, payment_method_id: str = "") -> None:
    with closing(_connect()) as db:
        extra = ", payment_method_id = COALESCE(NULLIF(?, ''), payment_method_id)" if payment_method_id else ""
        params = [status]
        if payment_method_id:
            params.append(payment_method_id)
        params.append(yookassa_id)
        sql = f"UPDATE payments SET status = ?, updated_at = datetime('now'){extra} WHERE yookassa_id = ?"
        db.execute(sql, params)
        db.commit()


def get_payment_record(yookassa_id: str) -> dict[str, Any] | None:
    """Получить запись платежа из локальной БД."""
    with closing(_connect()) as db:
        row = db.execute(
            "SELECT * FROM payments WHERE yookassa_id = ?", (yookassa_id,)
        ).fetchone()
    return dict(row) if row else None


def activate_subscription(
    payment_id: int,
    product_id: str,
    customer_email: str,
    payment_method_id: str,
    period_days: int = 30,
) -> int:
    with closing(_connect()) as db:
        expires = (datetime.now(timezone.utc) + timedelta(days=period_days)).isoformat()
        cur = db.execute(
            """INSERT INTO subscriptions (payment_id, product_id, customer_email, payment_method_id, status, expires_at)
               VALUES (?, ?, ?, ?, 'active', ?)""",
            (payment_id, product_id, customer_email, payment_method_id, expires),
        )
        db.commit()
        return cur.lastrowid


# ---------------------------------------------------------------------------
# ЮKassa API wrapper
# ---------------------------------------------------------------------------

_YOOKASSA_API = "https://api.yookassa.ru/v3"


def _auth_header() -> str:
    """Basic auth: base64(ShopId:SecretKey)"""
    raw = f"{config.YOOKASSA_SHOP_ID}:{config.YOOKASSA_SECRET_KEY}"
    return "Basic " + base64.b64encode(raw.encode("utf-8")).decode("ascii")


def _idempotency_key() -> str:
    return secrets.token_hex(16)


async def create_payment(
    product_id: str,
    amount_rub: int,
    description: str = "",
    return_url: str = "",
    capture: bool = True,
) -> dict[str, Any]:
    """Create a ЮKassa payment with redirect confirmation.
    In sandbox mode returns a fake confirmation_url."""
    prod = PRODUCTS.get(product_id)
    if prod is None:
        raise ValueError(f"Unknown product: {product_id}")

    if config.IS_YOOKASSA_SANDBOX:
        # Demo mode — fake response
        fake_id = f"sandbox-{secrets.token_hex(8)}"
        return {
            "id": fake_id,
            "status": "pending",
            "paid": False,
            "amount": {"value": f"{amount_rub}.00", "currency": "RUB"},
            "confirmation": {
                "type": "redirect",
                # Относительная ссылка: демо-цикл работает при любом способе доступа
                # (nginx/staging, прямой IP:порт, SSH-туннель) и не зависит от PUBLIC_BASE_URL.
                "confirmation_url": f"/api/payment-demo?yookassa_id={fake_id}&product_id={product_id}",
            },
            "test": True,
            "description": description or f"Оплата: {prod['name']}",
        }

    body = {
        "amount": {"value": f"{amount_rub}.00", "currency": "RUB"},
        "capture": capture,
        "confirmation": {
            "type": "redirect",
            "return_url": return_url or config.YOOKASSA_RETURN_URL,
        },
        "description": description or f"Оплата: {prod['name']}",
        "metadata": {"product_id": product_id},
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{_YOOKASSA_API}/payments",
            json=body,
            headers={
                "Authorization": _auth_header(),
                "Idempotence-Key": _idempotency_key(),
                "Content-Type": "application/json",
            },
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"ЮKassa error {resp.status_code}: {resp.text}")
        return resp.json()


async def get_payment(yookassa_id: str) -> dict[str, Any]:
    """Get payment status from ЮKassa."""
    if yookassa_id.startswith("sandbox-"):
        return {"id": yookassa_id, "status": "succeeded", "paid": True, "test": True}

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{_YOOKASSA_API}/payments/{yookassa_id}",
            headers={"Authorization": _auth_header()},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"ЮKassa error {resp.status_code}: {resp.text}")
        return resp.json()


def verify_webhook_signature(
    body_bytes: bytes,
    signature: str,
    max_age_seconds: int = 300,
) -> bool:
    """Verify ЮKassa webhook HMAC signature (if secret_key available).
    Returns True if verification passes or in sandbox mode."""
    if config.IS_YOOKASSA_SANDBOX:
        return True
    if not config.YOOKASSA_SECRET_KEY:
        return False
    expected = hmac.new(
        config.YOOKASSA_SECRET_KEY.encode("utf-8"),
        body_bytes,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)