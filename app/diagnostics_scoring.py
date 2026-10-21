"""Интерпретация результатов диагностики (методология /opt/alt_docs/alt_diagnostics/interpretation.md).

Бесплатная анкета — 4 шкалы 0–100 (вес каждой 25%):
риск блокировок / риск комплаенса / потенциал автоматизации / готовность к миграции.
Итог: 🟢 0–30 · 🟡 31–60 · 🔴 61–100.
«Ваше дело» — триггеры-рекомендации по выбранным болевым точкам.
"""

from __future__ import annotations

from typing import Any

LEVELS = {"green": "🟢 Низкий", "yellow": "🟡 Средний", "red": "🔴 Высокий"}


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]


def _scale(points: float) -> int:
    return max(0, min(100, round(points)))


def score_free(answers: dict[str, Any]) -> dict[str, Any]:
    """Скоринг бесплатной диагностики по 4 шкалам."""
    a = {k: (v if v is not None else "") for k, v in answers.items()}
    messengers = _as_list(a.get("messengers"))
    foreign = [p for p in str(a.get("foreign_platforms") or "").replace(",", " ").split() if p]

    # 1. Риск блокировок (25%): WhatsApp/Telegram +20, зарубежные платформы +15×N (макс 45), блокировки были +30
    blocked = 0
    if any(m in ("WhatsApp", "Telegram", "Instagram*") for m in messengers):
        blocked += 20
    blocked += min(45, 15 * len(foreign))
    if str(a.get("blocked_before")).lower() in ("да", "yes", "true", "1"):
        blocked += 30

    # 2. Риск комплаенса (25%): ПДн +20, 152-ФЗ не знают +30 / не уверены +20
    compliance = 0
    if str(a.get("pdn")).lower() in ("да", "yes", "true", "1"):
        compliance += 20
    fz = str(a.get("fz152")).lower()
    if "впервые" in fz or fz == "слышали":
        compliance += 30
    elif "не уверены" in fz:
        compliance += 20

    # 3. Потенциал автоматизации (25%): нет ботов +15, нет ИИ +15, рутина/цель автоматизации +15
    automation = 0
    if str(a.get("bots")).lower() == "нет":
        automation += 15
    if str(a.get("ai_tools")).lower() in ("нет, но интересно", "нет"):
        automation += 15
    if "автоматиза" in str(a.get("goal6m", "")).lower():
        automation += 15

    # 4. Готовность к миграции (25%): уже используют отечественные +20, планируют +10, бюджет есть +15
    readiness = 0
    if str(a.get("ru_it")).lower() in ("яндекс 360", "vk", "max", "rutube/rustore"):
        readiness += 20
    elif "хотим" in str(a.get("ru_it", "")).lower():
        readiness += 10
    if str(a.get("budget")) in ("15–75 тыс. ₽", "от 75 тыс. ₽"):
        readiness += 15

    scales = {
        "Риск блокировок": _scale(blocked),
        "Риск комплаенса": _scale(compliance),
        "Потенциал автоматизации": _scale(automation),
        "Готовность к миграции": _scale(readiness),
    }
    total = _scale(sum(scales.values()) / len(scales))
    level = "green" if total <= 30 else ("yellow" if total <= 60 else "red")
    return {"scales": scales, "total": total, "level": level, "level_text": LEVELS[level]}


TRIGGERS_YOUR_BUSINESS = {
    "Блокировки": ("Миграция на MAX/VK", "Перенесём каналы и сообщества без потери аудитории."),
    "Риски штрафов": ("Консультация по 152-ФЗ", "Проверим формы согласий, политику ПДн и уведомление РКН."),
    "Нет времени": ("Чат-бот / ИИ-ассистент", "Автоматизируем ответы на типовые вопросы (ВежПом / НейроСотрудник)."),
    "Сложности с платежами": ("Приём платежей", "Настроим ЮKassa/СБП под ваш сценарий продаж."),
    "Риск потери данных": ("Резервирование и миграция", "Перенесём данные в надёжное российское хранилище."),
    "Хочу разобраться в ИИ": ("АЛТ-эксперт + демонстрация", "Покажем, как ИИ увеличивает прибыль на ваших задачах."),
}


def triggers_your_business(answers: dict[str, Any]) -> list[dict[str, str]]:
    pains = _as_list(answers.get("pain_points"))
    out = []
    for pain in pains:
        for key, (title, desc) in TRIGGERS_YOUR_BUSINESS.items():
            if pain.startswith(key):
                out.append({"trigger": key, "recommendation": title, "description": desc})
                break
    return out


def summarize(kind: str, answers: dict[str, Any]) -> dict[str, Any]:
    """Единая точка: результат диагностики для письма/PDF/админки."""
    if kind == "free":
        return {"type": "scores", **score_free(answers)}
    if kind == "your_business":
        return {"type": "triggers", "items": triggers_your_business(answers)}
    return {"type": "paid_report"}
