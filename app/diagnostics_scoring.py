"""Интерпретация результатов диагностики.

Бесплатная анкета — профиль потерь по 5 зонам (ZeroCoder) + шкалы рисков как бонус (20%).
«Ваше дело» — каскад уточнений и триггеры-рекомендации.
"""
from __future__ import annotations

import re
from typing import Any



from typing import Any

LEVELS = {"green": "🟢 Низкий", "yellow": "🟡 Средний", "red": "🔴 Высокий"}

# ═══ ТЗ 02.11 (ZeroCoder): 5 зон потерь бизнеса ═══
# 80% внимания — где бизнес теряет время/деньги/клиентов; 20% — риски/комплаенс.

# Ключевые слова ответов для расчёта потерь по каждой зоне
LOSS_ZONE_KEYWORDS = {
    "Входящие заявки": {
        "channels_many": 20,      # несколько каналов без единой точки
        "no_single_point": 25,    # нет единой точки входа
        "slow_reply": 30,         # отвечаем медленно
        "lost_leads": 25,         # заявки теряются
    },
    "Ручная работа продаж": {
        "manual_crm": 30,         # ручной ввод в CRM
        "no_followup": 30,        # нет follow-up
        "notes_chaos": 20,        # заметки вразброс
    },
    "Клиентская поддержка": {
        "many_same_questions": 35,  # много однотипных вопросов
        "no_kb": 25,               # нет базы знаний
        "queue_mix": 20,           # простые ждут со сложными
    },
    "Поиск информации": {
        "docs_scattered": 35,      # документы в разных местах
        "colleague_interruptions": 30,  # коллеги отвлекают вопросами
        "no_regulations": 15,      # нет регламентов
    },
    "Отчётность и аналитика": {
        "manual_reports": 40,      # ручной сбор отчётов
        "hours_monthly": 30,       # много часов в месяц
        "no_auto_dash": 15,        # нет автоматических дашбордов
    },
}

# Для каждой зоны: гипотеза + MVP-шаг (ZeroCoder: проблема → причина → MVP)
ZONE_SOLUTIONS = {
    "Входящие заявки": {
        "hypothesis": "Сбор заявок в единой точке + ИИ-квалификация вернёт заметную часть потерянных контактов",
        "mvp_step": "Подключите ОДИН канал к тестовой системе обработки заявок на неделю — замерьте скорость первого ответа и % потерянных до/после (демо: rusalts.ru/autoprocessing)",
        "layer": "ИИ + автоматизация",
        "product": ("Автоматизация обработки заявок (демо-пилот)", "https://rusalts.ru/autoprocessing"),
    },
    "Ручная работа продаж": {
        "hypothesis": "ИИ-ассистент возьмёт на себя ввод, квалификацию и follow-up — освободит время на живые продажи",
        "mvp_step": "Тестовый ИИ-ассистент на одном этапе воронки (например, только квалификация входящих) на 1–2 недели",
        "layer": "ИИ + автоматизация",
        "product": ("НейроСотрудник", "https://rusalts.ru/techologis/"),
    },
    "Клиентская поддержка": {
        "hypothesis": "ИИ-первая линия закроет 70–80% типовых вопросов, сложные — попадут к человеку",
        "mvp_step": "Подключите ассистента с базой из 10 самых частых вопросов — замерьте % решённых без человека за неделю",
        "layer": "ИИ",
        "product": ("НейроСотрудник", "https://rusalts.ru/techologis/"),
    },
    "Поиск информации": {
        "hypothesis": "AI-агент с базой знаний ответит на 90% внутренних вопросов без отрыва коллег",
        "mvp_step": "Соберите 5 самых частых внутренних вопросов и проверьте ответы через агента за 1 неделю",
        "layer": "ИИ",
        "product": ("OpenClaw AI-агент", "по запросу: v.devops@yandex.ru"),
    },
    "Отчётность и аналитика": {
        "hypothesis": "Автосбор метрик в дашборд уберёт ручную отчётность и даст данные в реальном времени",
        "mvp_step": "Автоматизируйте ОДИН регулярный отчёт (самый трудозатратный) — сравните время до/после",
        "layer": "Автоматизация + ИИ",
        "product": ("OpenClaw AI-агент", "по запросу: v.devops@yandex.ru"),
    },
}


def _as_bool(value: Any) -> bool:
    return str(value or "").strip().lower() in ("да", "yes", "true", "1", "есть")


def _int_or_zero(value: Any) -> int:
    try:
        return int(re.sub(r"[^0-9]", "", str(value or "")) or 0)
    except ValueError:
        return 0


def compute_loss_profile(answers: dict[str, Any]) -> dict[str, Any]:
    """Профиль потерь по 5 зонам (ТЗ 02.11): 0–100 каждая, top_loss, причина,
    гипотеза и MVP-шаг от причины (ZeroCoder: проблема → причина → решение)."""
    import re as _re

    def has(key: str) -> bool:
        return any(
            _as_bool(a.get(key)) or str(a.get(key) or "").lower().find(str(v).lower()) >= 0
            for v in [LOSS_ZONE_KEYWORDS]
        ) and False  # placeholder — реальные проверки ниже

    a = {k: (v if v is not None else "") for k, v in answers.items()}
    channels = len(_as_list(a.get("lead_channels")))
    leads_per_day = _int_or_zero(a.get("leads_per_day"))
    reply_hours = _int_or_zero(a.get("reply_hours"))
    crm_hours_week = _int_or_zero(a.get("crm_hours_week"))
    same_questions_day = _int_or_zero(a.get("same_questions_day"))
    report_hours_month = _int_or_zero(a.get("report_hours_month"))

    # Зона 1: Входящие заявки
    z_leads = 0
    if channels >= 2:
        z_leads += LOSS_ZONE_KEYWORDS["Входящие заявки"]["channels_many"]
    if str(a.get("single_entry", "")).lower() in ("нет", "no"):
        z_leads += LOSS_ZONE_KEYWORDS["Входящие заявки"]["no_single_point"]
    if reply_hours >= 2:
        z_leads += LOSS_ZONE_KEYWORDS["Входящие заявки"]["slow_reply"]
    if str(a.get("lost_leads", "")).lower() in ("да", "yes", "иногда"):
        z_leads += LOSS_ZONE_KEYWORDS["Входящие заявки"]["lost_leads"]
    if leads_per_day >= 10 and channels >= 2:
        z_leads += 15  # объём усиливает потери

    # Зона 2: Ручная работа продаж
    z_sales = 0
    if crm_hours_week >= 3:
        z_sales += LOSS_ZONE_KEYWORDS["Ручная работа продаж"]["manual_crm"]
    if str(a.get("followup", "")).lower() in ("нет", "no", "иногда"):
        z_sales += LOSS_ZONE_KEYWORDS["Ручная работа продаж"]["no_followup"]
    if str(a.get("notes_chaos", "")).lower() in ("да", "yes"):
        z_sales += LOSS_ZONE_KEYWORDS["Ручная работа продаж"]["notes_chaos"]

    # Зона 3: Клиентская поддержка
    z_support = 0
    if same_questions_day >= 10:
        z_support += LOSS_ZONE_KEYWORDS["Клиентская поддержка"]["many_same_questions"]
    if str(a.get("has_kb", "")).lower() in ("нет", "no"):
        z_support += LOSS_ZONE_KEYWORDS["Клиентская поддержка"]["no_kb"]
    if str(a.get("queue_mix", "")).lower() in ("да", "yes"):
        z_support += LOSS_ZONE_KEYWORDS["Клиентская поддержка"]["queue_mix"]

    # Зона 4: Поиск информации
    z_info = 0
    if str(a.get("docs_location", "")).lower() in ("в разных местах", "разные места", "хаотично"):
        z_info += LOSS_ZONE_KEYWORDS["Поиск информации"]["docs_scattered"]
    if _int_or_zero(a.get("interruptions_day")) >= 3:
        z_info += LOSS_ZONE_KEYWORDS["Поиск информации"]["colleague_interruptions"]
    if str(a.get("has_regulations", "")).lower() in ("нет", "no"):
        z_info += LOSS_ZONE_KEYWORDS["Поиск информации"]["no_regulations"]

    # Зона 5: Отчётность
    z_reports = 0
    if str(a.get("reports_how", "")).lower().startswith("вручную"):
        z_reports += LOSS_ZONE_KEYWORDS["Отчётность и аналитика"]["manual_reports"]
    if report_hours_month >= 8:
        z_reports += LOSS_ZONE_KEYWORDS["Отчётность и аналитика"]["hours_monthly"]
    if str(a.get("has_dashboard", "")).lower() in ("нет", "no"):
        z_reports += LOSS_ZONE_KEYWORDS["Отчётность и аналитика"]["no_auto_dash"]

    scales_loss = {
        "Входящие заявки": _scale(z_leads),
        "Ручная работа продаж": _scale(z_sales),
        "Клиентская поддержка": _scale(z_support),
        "Поиск информации": _scale(z_info),
        "Отчётность и аналитика": _scale(z_reports),
    }

    top_loss = max(scales_loss, key=scales_loss.get)
    top_value = scales_loss[top_loss]

    # Причина: формулируем по ответам в зоне-лидере
    reasons = []
    if top_loss == "Входящие заявки":
        if channels >= 2:
            reasons.append(f"{channels} канала(-ов) заявок без единой точки входа")
        if reply_hours >= 2:
            reasons.append(f"первый ответ в среднем {reply_hours} ч")
        if str(a.get("lost_leads", "")).lower() == "да":
            reasons.append("заявки теряются")
    elif top_loss == "Ручная работа продаж":
        if crm_hours_week >= 3:
            reasons.append(f"{crm_hours_week} ч/нед на ручной ввод")
        if str(a.get("followup", "")).lower() == "нет":
            reasons.append("follow-up не ведётся")
    elif top_loss == "Клиентская поддержка":
        if same_questions_day >= 10:
            reasons.append(f"{same_questions_day}+ однотипных вопросов в день")
        if str(a.get("has_kb", "")).lower() == "нет":
            reasons.append("нет базы знаний")
    elif top_loss == "Поиск информации":
        if str(a.get("docs_location", "")):
            reasons.append(f"документы: {a.get('docs_location')}")
        if _int_or_zero(a.get("interruptions_day")) >= 3:
            reasons.append(f"{a.get('interruptions_day')}+ отвлечений в день")
    elif top_loss == "Отчётность и аналитика":
        if str(a.get("reports_how", "")).lower().startswith("вручную"):
            reasons.append("отчётность собирается вручную")
        if report_hours_month >= 8:
            reasons.append(f"{report_hours_month}+ ч/мес на отчёты")

    reason_txt = "; ".join(reasons) if reasons else "по ответам анкеты зона выражена"

    sol = ZONE_SOLUTIONS[top_loss]
    hypothesis = (
        f"{top_loss}: {reason_txt}. "
        f"Гипотеза: {sol['hypothesis']}."
    )
    mvp_step = f"MVP-проверка (не внедрение!): {sol['mvp_step']}"

    return {
        "loss_profile": scales_loss,
        "top_loss": top_loss,
        "top_loss_value": top_value,
        "top_loss_reason": reason_txt,
        "hypothesis": hypothesis,
        "mvp_step": mvp_step,
        "solution_layer": sol["layer"],
        "product_ref": {"name": sol["product"][0], "link": sol["product"][1]},
    }



# Рекомендации по шкалам (B2): порог → текст (для рисков высокий балл = проблема,
# для потенциала/готовности высокий балл = возможность).
RECOMMENDATIONS = {
    "Риск блокировок": [
        (60, "Критично: каналы могут заблокировать. Плановая миграция аудитории на MAX/VK — продукт «Оживление» и сопровождение АЛТ."),
        (30, "Есть уязвимость: зависимость от зарубежных платформ. Продумайте резервный канал в MAX/VK."),
    ],
    "Риск комплаенса": [
        (60, "Критично: вероятны штрафы по 152-ФЗ. Аудит персональных данных и приведение документов в порядок — в первую очередь."),
        (30, "Есть пробелы в комплаенсе: проверьте согласия на обработку ПДн и политику на сайте."),
    ],
    "Потенциал автоматизации": [
        (30, "Хороший задел: рутинные ответы и рассылки можно передать ИИ-ассистенту (НейроСотрудник, ВежПом) — экономия часов в неделю."),
        (15, "Есть процессы для пилота: начните с одной задачи — типовые ответы клиентам."),
    ],
    "Готовность к миграции": [
        (30, "Вы уже частично на отечественных сервисах — миграция пройдёт быстро и без потерь."),
        (15, "Начните с малого: перенесите один процесс (почта или чат) — остальное по дорожной карте."),
    ],
}


def _recommendations_for(scales: dict[str, int]) -> list[str]:
    out = []
    for scale, value in scales.items():
        for threshold, text in RECOMMENDATIONS.get(scale, []):
            if value >= threshold:
                out.append(f"{scale}: {text}")
                break
    return out


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

    # ТЗ 02.11: профиль потерь — главный блок; риски — бонус надёжности (20%)
    loss = compute_loss_profile(answers)
    recs = loss.get("recommendations") or _recommendations_for(scales)

    return {
        "scales": scales,
        "total": total,
        "level": level,
        "level_text": LEVELS[level],
        "recommendations": recs,
        "loss_profile": loss["loss_profile"],
        "top_loss": loss["top_loss"],
        "top_loss_reason": loss["top_loss_reason"],
        "hypothesis": loss["hypothesis"],
        "mvp_step": loss["mvp_step"],
        "solution_layer": loss["solution_layer"],
        "product_ref": loss["product_ref"],
    }


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
