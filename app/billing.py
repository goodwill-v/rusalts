"""Billing engine: token-based subscription tracking for the Techologis showcase.

Architecture:
  ┌──────────────────────────────────────────────────────────────────────┐
  │  rusalts.ru/techologis/ (FastAPI)                                    │
  │                                                                      │
  │  payments.py ──► ЮKassa (приём платежей)                            │
  │  billing.py  ──► SQLite (тарифы, токены, подписки)                  │
  │  routerai.py ──► RouterAI API (учёт расхода токенов по ключам)      │
  │                                                                      │
  │  После успешного платежа:                                            │
  │    1. payment.succeeded (вебхук)                                     │
  │    2. billing.activate_subscription() — зачисляем токены             │
  │    3. billing.deduct_tokens() — каждый вызов ИИ списывает токены     │
  │    4. billing.check_balance() — предупреждение при низком остатке    │
  └──────────────────────────────────────────────────────────────────────┘

Token tracking:
  - Каждый ИИ-запрос через RouterAI возвращает usage.prompt_tokens
    и usage.completion_tokens в штатном OpenAI-формате.
  - Мы записываем расход в таблицу token_usage (customer + product + tokens).
  - Баланс = лимит подписки − сумма израсходованных токенов.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from app import config

# ═══════════════════════════════════════════════════════════════════════
# TARIFFS — тарифные планы продуктов (цены в рублях, токены)
# ═══════════════════════════════════════════════════════════════════════
#
# Каждый продукт использует определённую модель RouterAI.
# Тариф = цена подписки + лимит токенов на месяц.
# Модели RouterAI тарифицируются за 1 млн токенов (вход + выход отдельно).
# Цены моделей — из каталога routerai.ru/models (в рублях).
#
# Схема расчета:
#   Стоимость токенов для нас = prompt_tokens × price_prompt_per_token
#                                + completion_tokens × price_completion_per_token
#   Маржа = цена_подписки − стоимость_токенов_для_нас
#
# ⚠️ Цены моделей меняются. Обновляйте TARIFFS при изменении прайса RouterAI.

@dataclass
class Tariff:
    """Тарифный план продукта."""
    product_id: str
    name: str
    price_rub: int                    # Цена подписки для клиента (₽/мес)
    token_allowance: int              # Базовое количество токенов в подписке
    routerai_model_id: str            # Идентификатор модели на RouterAI
    routerai_prompt_price_per_1m: float   # Цена за 1 млн входных токенов (₽)
    routerai_completion_price_per_1m: float  # Цена за 1 млн выходных токенов (₽)
    token_cost_ratio: float           # Доля цены подписки на покрытие токенов (≈0.5-0.7)
    estimated_dialogues: str          # Примерное кол-во диалогов (для витрины)
    trial_tokens: int = 0             # Токенов для пробного периода (7 дней)
    period_days: int = 30             # Дней действия подписки
    additional_token_price_per_1m: float = 0  # Цена докупки 1 млн токенов (₽)

    @property
    def monthly_token_budget_rub(self) -> float:
        """Сколько рублей из подписки уходит на оплату токенов RouterAI."""
        return round(self.price_rub * self.token_cost_ratio, 2)

    @property
    def margin_rub(self) -> float:
        """Наша маржа с одной подписки (₽/мес)."""
        return round(self.price_rub - self.monthly_token_budget_rub, 2)

    def estimate_prompt_tokens_per_dialogue(self, turns: int = 6) -> int:
        """Примерная оценка: X токенов на диалог средней длины."""
        return min(turns * 400, self.token_allowance // 5)


# ── Тарифы по продуктам ──────────────────────────────────────────────
# Цены моделей RouterAI (сентябрь 2026, рубли за 1M токенов):
#   GPT-4o-mini:         ≈ 2.0  ₽ вход / 8.0  ₽ выход
#   GPT-4o:              ≈ 20.0 ₽ вход / 60.0 ₽ выход
#   GigaChat:            ≈ 3.0  ₽ вход / 9.0  ₽ выход
#   Claude Sonnet 4.5:   ≈ 30.0 ₽ вход / 150.0 ₽ выход
#   DeepSeek V3:         ≈ 1.5  ₽ вход / 4.0  ₽ выход
# ⚠️ Актуальные цены: https://routerai.ru/models

TARIFFS: dict[str, Tariff] = {
    "italidia": Tariff(
        product_id="italidia",
        name="ItaLidia — ассистент итальянского языка",
        price_rub=300,
        token_allowance=900_000,        # 900K токенов на месяц (Шеф, 04.10)
        routerai_model_id="openai/gpt-4o-mini",
        routerai_prompt_price_per_1m=2.0,
        routerai_completion_price_per_1m=8.0,
        token_cost_ratio=0.60,           # 180₽ на токены, 120₽ маржа
        estimated_dialogues="от 80 до 400",
        trial_tokens=200_000,         # 200K токенов на пробу (~7 дней)
        additional_token_price_per_1m=10.0,  # Докупка по 10₽/1M токенов
    ),
    "vezhpom": Tariff(
        product_id="vezhpom",
        name="ВежПом — мониторинг серверов",
        price_rub=150,
        token_allowance=100_000,
        routerai_model_id="openai/gpt-4o-mini",
        routerai_prompt_price_per_1m=2.0,
        routerai_completion_price_per_1m=8.0,
        token_cost_ratio=0.30,           # 45₽ на токены, 105₽ маржа
        estimated_dialogues="до 200 проверок и уведомлений",
        trial_tokens=50_000,
        additional_token_price_per_1m=8.0,
    ),
    "neyrosotrudnik": Tariff(
        product_id="neyrosotrudnik",
        name="НейроСотрудник — ИИ-ассистент",
        price_rub=500,
        token_allowance=800_000,
        routerai_model_id="openai/gpt-4o-mini",
        routerai_prompt_price_per_1m=2.0,
        routerai_completion_price_per_1m=8.0,
        token_cost_ratio=0.50,           # 250₽ на токены, 250₽ маржа
        estimated_dialogues="от 150 до 700",
        trial_tokens=375_000,
        additional_token_price_per_1m=10.0,
    ),
    "alt_expert": Tariff(
        product_id="alt_expert",
        name="АЛТ-эксперт — консультант",
        price_rub=0,
        token_allowance=50_000,          # Бесплатно, ограниченный лимит
        routerai_model_id="openai/gpt-4o-mini",
        routerai_prompt_price_per_1m=2.0,
        routerai_completion_price_per_1m=8.0,
        token_cost_ratio=0.0,            # Бесплатно для пользователя
        estimated_dialogues="до 30 консультаций",
        trial_tokens=0,
        additional_token_price_per_1m=0,
    ),
    "italidia_test": Tariff(
        product_id="italidia_test",
        name="ItaLidia — тестовый (4 дня)",
        price_rub=300,
        token_allowance=120_000,          # 120K = 900K × 4/30
        routerai_model_id="openai/gpt-4o-mini",
        routerai_prompt_price_per_1m=2.0,
        routerai_completion_price_per_1m=8.0,
        token_cost_ratio=0.60,
        estimated_dialogues="тестовый режим, 4 дня",
        trial_tokens=30_000,              # 30K = 120K × 1/4 (1 день из 4)
        period_days=4,
        additional_token_price_per_1m=10.0,
    ),
}


def get_tariff(product_id: str) -> Tariff | None:
    """Вернуть тариф продукта или None."""
    return TARIFFS.get(product_id)


def format_token_allowance(tokens: int) -> str:
    """Форматировать токены в человекочитаемый вид."""
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}M"
    if tokens >= 1000:
        return f"{tokens // 1000}K"
    return str(tokens)


def get_subscription_text(product_id: str) -> str:
    """Сгенерировать текст описания подписки для пользователя (для витрины)."""
    t = get_tariff(product_id)
    if t is None or t.price_rub == 0:
        return "Бесплатно · Безлимитный доступ"
    
    allowance = format_token_allowance(t.token_allowance)
    return (
        f"Приобретая месячную подписку за {t.price_rub} ₽, "
        f"вы получаете ссылку для перехода на бота, "
        f"вам начисляется базовое количество токенов ({allowance}) "
        f"в соответствии с тарифами модели {t.routerai_model_id}. "
        f"Это позволяет провести {t.estimated_dialogues} полноценных консультаций, "
        f"диалогов, запросов (это зависит от длительности разговора, "
        f"отправки графических файлов и других факторов). "
        f"Если токены израсходованы, вы сохраняете доступ к итогам прошлых сессий. "
        f"Для продления работы можно докупить токены "
        f"(Продлить подписку на месяц от текущей даты)."
    )


# ═══════════════════════════════════════════════════════════════════════
# TOKEN USAGE TRACKING — учёт расхода токенов по клиентам
# ═══════════════════════════════════════════════════════════════════════

def _connect_usage() -> sqlite3.Connection:
    """Соединение к БД учёта токенов."""
    db = sqlite3.connect(str(config.DATA_DIR / "billing.db"))
    db.row_factory = sqlite3.Row
    db.execute("""
        CREATE TABLE IF NOT EXISTS token_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id TEXT NOT NULL,
            product_id TEXT NOT NULL,
            model TEXT NOT NULL DEFAULT '',
            prompt_tokens INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            cost_rub REAL NOT NULL DEFAULT 0,
            recorded_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS token_balances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id TEXT NOT NULL,
            product_id TEXT NOT NULL,
            subscription_id INTEGER NOT NULL DEFAULT 0,
            allowance_tokens INTEGER NOT NULL DEFAULT 0,
            used_tokens INTEGER NOT NULL DEFAULT 0,
            expires_at TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            UNIQUE(customer_id, product_id, subscription_id)
        )
    """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_usage_customer
        ON token_usage(customer_id, product_id, recorded_at)
    """)
    return db


def get_or_create_balance(
    customer_id: str,
    product_id: str,
    allowance_tokens: int,
    subscription_id: int = 0,
    period_days: int = 30,
) -> dict[str, Any]:
    """Получить баланс клиента или создать новый."""
    with closing(_connect_usage()) as db:
        row = db.execute(
            """SELECT * FROM token_balances
               WHERE customer_id = ? AND product_id = ?
               AND status = 'active'
               ORDER BY id DESC LIMIT 1""",
            (customer_id, product_id),
        ).fetchone()

        if row:
            return dict(row)

        expires = (datetime.now(timezone.utc) + timedelta(days=period_days)).isoformat()
        cur = db.execute(
            """INSERT INTO token_balances
               (customer_id, product_id, subscription_id, allowance_tokens, used_tokens, expires_at, status)
               VALUES (?, ?, ?, ?, 0, ?, 'active')""",
            (customer_id, product_id, subscription_id, allowance_tokens, expires),
        )
        db.commit()
        return {
            "customer_id": customer_id,
            "product_id": product_id,
            "subscription_id": subscription_id,
            "allowance_tokens": allowance_tokens,
            "used_tokens": 0,
            "expires_at": expires,
            "status": "active",
        }


def record_usage(
    customer_id: str,
    product_id: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost_rub: float = 0,
) -> dict[str, Any]:
    """Записать расход токенов и обновить баланс. Вернуть остаток."""
    with closing(_connect_usage()) as db:
        db.execute(
            """INSERT INTO token_usage
               (customer_id, product_id, model, prompt_tokens, completion_tokens, cost_rub)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (customer_id, product_id, model, prompt_tokens, completion_tokens, cost_rub),
        )
        total_tokens = prompt_tokens + completion_tokens
        db.execute(
            """UPDATE token_balances
               SET used_tokens = used_tokens + ?
               WHERE customer_id = ? AND product_id = ? AND status = 'active'""",
            (total_tokens, customer_id, product_id),
        )
        db.commit()

        row = db.execute(
            """SELECT * FROM token_balances
               WHERE customer_id = ? AND product_id = ? AND status = 'active'
               ORDER BY id DESC LIMIT 1""",
            (customer_id, product_id),
        ).fetchone()
        return dict(row) if row else {}


def get_balance(customer_id: str, product_id: str) -> dict[str, Any] | None:
    """Текущий баланс клиента по продукту."""
    with closing(_connect_usage()) as db:
        row = db.execute(
            """SELECT * FROM token_balances
               WHERE customer_id = ? AND product_id = ? AND status = 'active'
               ORDER BY id DESC LIMIT 1""",
            (customer_id, product_id),
        ).fetchone()
        return dict(row) if row else None


def get_remaining_tokens(customer_id: str, product_id: str) -> int:
    """Сколько токенов осталось у клиента."""
    bal = get_balance(customer_id, product_id)
    if bal is None:
        return 0
    return bal["allowance_tokens"] - bal["used_tokens"]


def remaining_pct(customer_id: str, product_id: str) -> float:
    """Процент оставшихся токенов (0.0–1.0). 0 если нет подписки."""
    bal = get_balance(customer_id, product_id)
    if bal is None or bal["allowance_tokens"] == 0:
        return 0.0
    remaining = bal["allowance_tokens"] - bal["used_tokens"]
    return max(0.0, remaining / bal["allowance_tokens"])


def usage_pct(customer_id: str, product_id: str) -> float:
    """Процент использованных токенов (0.0–1.0)."""
    return 1.0 - remaining_pct(customer_id, product_id)


def is_balance_low(customer_id: str, product_id: str, threshold_pct: float = 0.20) -> bool:
    """Баланс ниже порога (по умолчанию 20%)? Пора предупредить клиента."""
    return remaining_pct(customer_id, product_id) < threshold_pct


def check_before_inference(customer_id: str, product_id: str) -> dict:
    """Проверить, можно ли выполнить AI-запрос.

    Returns:
        {"ok": True} — запрос разрешён.
        {"ok": False, "reason": "exhausted", "message": "..."} — лимит исчерпан.
        {"ok": False, "reason": "no_subscription", "message": "..."} — нет подписки.
        {"ok": False, "reason": "expired", "message": "..."} — срок истёк.
    """
    bal = get_balance(customer_id, product_id)
    if bal is None:
        return {
            "ok": False,
            "reason": "no_subscription",
            "message": "Подписка не найдена. Пожалуйста, оформите подписку.",
        }
    if bal["status"] == "expired":
        return {
            "ok": False,
            "reason": "expired",
            "message": "Срок подписки истёк. Пожалуйста, продлите подписку.",
        }
    remaining = bal["allowance_tokens"] - bal["used_tokens"]
    if remaining <= 0:
        return {
            "ok": False,
            "reason": "exhausted",
            "message": "Пожалуйста, продлите подписку. Базовый лимит исчерпан.",
            "warning_pct": usage_pct(customer_id, product_id) * 100,
        }
    return {"ok": True}
    bal = get_balance(customer_id, product_id)
    if bal is None:
        return True
    remaining = bal["allowance_tokens"] - bal["used_tokens"]
    if bal["allowance_tokens"] == 0:
        return True
    return (remaining / bal["allowance_tokens"]) < threshold_pct


def get_usage_summary(customer_id: str) -> list[dict[str, Any]]:
    """Сводка расхода по всем продуктам клиента."""
    with closing(_connect_usage()) as db:
        rows = db.execute(
            """SELECT product_id, SUM(prompt_tokens) as total_prompt,
                      SUM(completion_tokens) as total_completion,
                      SUM(cost_rub) as total_cost,
                      COUNT(*) as total_requests
               FROM token_usage
               WHERE customer_id = ?
               GROUP BY product_id""",
            (customer_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_expired_subscriptions() -> list[dict[str, Any]]:
    """Найти подписки, срок которых истёк (для cron-задачи деактивации)."""
    with closing(_connect_usage()) as db:
        rows = db.execute(
            """SELECT * FROM token_balances
               WHERE status = 'active' AND expires_at < datetime('now')"""
        ).fetchall()
        return [dict(r) for r in rows]


def deactivate_expired_subscriptions() -> int:
    """Деактивировать просроченные подписки. Вернуть количество."""
    with closing(_connect_usage()) as db:
        cur = db.execute(
            """UPDATE token_balances SET status = 'expired'
               WHERE status = 'active' AND expires_at < datetime('now')"""
        )
        db.commit()
        return cur.rowcount


def top_up_balance(
    customer_id: str,
    product_id: str,
    extra_tokens: int,
) -> int | None:
    """Добавить токены к активной подписке. Вернуть новый allowance или None."""
    with closing(_connect_usage()) as db:
        bal = db.execute(
            """SELECT * FROM token_balances
               WHERE customer_id = ? AND product_id = ? AND status = 'active'
               ORDER BY id DESC LIMIT 1""",
            (customer_id, product_id),
        ).fetchone()
        if not bal:
            return None
        new_allowance = bal["allowance_tokens"] + extra_tokens
        db.execute(
            """UPDATE token_balances SET allowance_tokens = ?
               WHERE id = ?""",
            (new_allowance, bal["id"]),
        )
        db.commit()
        return new_allowance


# ═══════════════════════════════════════════════════════════════════════
# ROUTERAI KEY STRATEGY
# ═══════════════════════════════════════════════════════════════════════
#
# RouterAI поддерживает два типа ключей:
#
# 1. API-ключ — для вызовов AI-моделей (/chat/completions)
#    • Можно установить кредитный лимит
#    • Создаётся через UI или через Мастер-ключ по API
#    • Каждый ответ RouterAI возвращает usage.prompt_tokens/completion_tokens
#
# 2. Мастер-ключ (Master Key) — для управления API-ключами программно
#    • Создаётся только через UI
#    • Позволяет: POST /keys (создать), GET /keys (список),
#      DELETE /keys/{hash} (удалить), POST /keys/{hash}/rotate (перевыпустить)
#    ⚠️ Мастер-ключом нельзя вызывать модели!
#
# ── Рекомендуемая стратегия для MVP ──────────────────────────────────
#
# 【Вариант A — единый ключ (рекомендую для старта)】
#   • Один API-ключ на все запросы клиентов (с высоким лимитом)
#   • Расход токенов считаем сами через billing.py (таблица token_usage)
#   • Плюсы: просто, 0 дополнительных запросов, мгновенный учёт
#   • Минусы: на одной стороне все клиенты (но это не проблема для MVP)
#
# 【Вариант B — мастер-ключ + пер-клиентские API-ключи (масштабирование)】
#   • Создать Мастер-ключ через UI (Настройки → Мастер-ключи)
#   • При покупке подписки — создать API-ключ с лимитом через POST /api/v1/keys
#   • API-ключ клиента зашить в его бота
#   • RouterAI сам остановит запросы при исчерпании лимита
#   • Плюсы: RouterAI сам контролирует лимиты, изоляция клиентов
#   • Минусы: нужно API-взаимодействие, поддержка OAuth
#
# Для MVP выбираем Вариант A. К Варианту B перейдём при масштабировании.

def get_routerai_key_type_description() -> str:
    """Вернуть описание типов ключей RouterAI для технической документации."""
    return """
RouterAI ключи для монетизации:

┌──────────────────────────┬─────────────────────────────┬──────────────────────┐
│ Тип ключа                │ Назначение                  │ Наш случай           │
├──────────────────────────┼─────────────────────────────┼──────────────────────┤
│ API-ключ                 │ Вызовы AI-моделей            │ Все запросы ИИ через │
│(с кредитным лимитом)     │ /chat/completions            │ этот ключ            │
│                          │ Создание: UI или API         │ Лимит: 10 000 ₽      │
├──────────────────────────┼─────────────────────────────┼──────────────────────┤
│ Мастер-ключ (Master Key) │ Управление API-ключами       │ Автоматическое       │
│                          │ /keys/* (создать/удалить)    │ создание ключей      │
│                          │ Создание: только через UI    │ для новых клиентов   │
│                          │                              │ (будущее)             │
└──────────────────────────┴─────────────────────────────┴──────────────────────┘

Рекомендация: для старта используем 1 API-ключ + свой учёт токенов.
Мастер-ключ — когда клиентов станет > 10.

Расход токенов считаем из ответа RouterAI (usage.prompt_tokens,
usage.completion_tokens) и записываем в нашу token_usage.
Стоимость рассчитываем по ценам из routerai.ru/pricing.
"""