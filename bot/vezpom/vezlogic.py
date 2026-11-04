#!/usr/bin/env python3
"""vezlogic.py — чистые функции классификации событий и анти-шума ВежПома.

Без env/сетей/БД: только разбор данных Kuma и решение «слать сейчас или нет».
Используется vezpom_webhook.py; покрыто tests/test_vezpom_logic.py (CI).

Форматы вебхуков Kuma 1.23.x, на которые рассчитан classify_event:
  * incident: {"monitor": {...}, "heartbeat": {"status": 0|1, "ping", "msg"}}
  * legacy:   {"status": "down"|"up", "previous_status": ..., "name": ...}
  * ssl:      {"monitor": {...}, "heartbeat": null,
               "msg": "[name][url] <тип> certificate <CN> will be expired in N days"}
  * backup:   как incident, но monitor.type == "push"
"""
import re

# Виды событий
KIND_INCIDENT = "incident"   # падение/восстановление сайта/сервера (http/ping/порт)
KIND_SSL = "ssl"             # срок SSL-сертификата (certificate expiry Kuma)
KIND_BACKUP = "backup"       # push-монитор резервных копий
KIND_DOMAIN = "domain"       # срок регистрации домена (domain_watch, отдельный PR)
KIND_OTHER = "other"

SSL_PATTERN = re.compile(r"will be expired in\s+(\d+)\s+days?", re.IGNORECASE)

# Тихие часы (МСК): некритичные сообщения не будим ночью, увидим в светофоре (PR-3)
QUIET_START = 23
QUIET_END = 8

# Пороги мгновенных сообщений о сроках домена (дней до истечения):
# одно сообщение на порог, дедуп в domain_watch через warnings.last_level
DOMAIN_THRESHOLDS = (30, 14, 7, 1)


def is_quiet_hour(msk_hour):
    """True в тихие часы 23:00–07:59 МСК."""
    return msk_hour >= QUIET_START or msk_hour < QUIET_END


def _heartbeat_detail(heartbeat):
    """Человекочитаемая деталь из heartbeat: отклик + текст ошибки."""
    heartbeat = heartbeat or {}
    parts = []
    ping = heartbeat.get("ping")
    if ping:
        parts.append("отклик %s мс" % ping)
    msg = heartbeat.get("msg") or ""
    if msg and msg not in ("OK", "Success", "200", "ok"):
        parts.append(str(msg)[:120])
    return " (" + ", ".join(parts) + ")" if parts else ""


def classify_event(data):
    """Kuma webhook payload → dict.

    Возвращает {kind, status, days, name, target, message, detail}:
      kind:   incident | ssl | backup | domain | other
      status: down | up | info
      days:   дней до истечения (ssl/domain), иначе None
      detail: деталь инцидента (отклик/ошибка) для строки сообщения
    """
    monitor = data.get("monitor") or {}
    heartbeat = data.get("heartbeat") or {}
    name = monitor.get("name") or data.get("name") or "?"
    target = monitor.get("url") or data.get("url") or ""
    msg = data.get("msg") or heartbeat.get("msg") or ""
    detail = _heartbeat_detail(heartbeat)

    # SSL: отдельное уведомление Kuma с текстом "will be expired in N days"
    m = SSL_PATTERN.search(str(msg))
    if m:
        return {
            "kind": KIND_SSL, "status": "info", "days": int(m.group(1)),
            "name": name, "target": target,
            "message": str(msg)[:200], "detail": "",
        }

    # Push-монитор резервных копий
    if monitor.get("type") == "push":
        status_num = heartbeat.get("status", 1)
        return {
            "kind": KIND_BACKUP,
            "status": "down" if status_num == 0 else "up",
            "days": None, "name": name, "target": "backup",
            "message": str(msg)[:200] if isinstance(msg, str) else "",
            "detail": detail,
        }

    # Обычный монитор (http/ping/порт) в формате v2
    if "monitor" in data:
        status_num = heartbeat.get("status", 1)
        return {
            "kind": KIND_INCIDENT,
            "status": "down" if status_num == 0 else "up",
            "days": None, "name": name, "target": target,
            "message": "", "detail": detail,
        }

    # Legacy-формат (v1): строковые статусы
    status = data.get("status", "")
    previous = data.get("previous_status", "")
    if status == "down":
        st = "down"
    elif status == "up" and previous == "down":
        st = "up"
    else:
        return {
            "kind": KIND_OTHER, "status": "info", "days": None,
            "name": name, "target": target,
            "message": str(status)[:100], "detail": detail,
        }
    return {
        "kind": KIND_INCIDENT, "status": st, "days": None,
        "name": name, "target": target, "message": "", "detail": detail,
    }


def should_forward(result, msk_hour, last_level=None):
    """Слать сообщение пользователю сразу (True) или оставить в журнале/светофоре?

    incident/backup — всегда сразу (это то, ради чего бот).
    ssl — вне тихих часов (Kuma сам шлёт один раз на порог 21/14/7).
    domain — только на новом пороге из DOMAIN_THRESHOLDS (30/14/7/1) и вне
    тихих часов; суточный обход доменов ведёт дедуп порогов сам
    (domain_watch.process_expiry), вебхуков-доменов Kuma 1.23 не шлёт.
    other — да (странное лучше показать).
    """
    kind = result.get("kind")
    if kind in (KIND_INCIDENT, KIND_BACKUP, KIND_OTHER):
        return True
    if kind == KIND_SSL:
        return not is_quiet_hour(msk_hour)
    if kind == KIND_DOMAIN:
        days = result.get("days")
        if days is None:
            return False
        if last_level is not None and days >= last_level:
            return False  # этот или более ранний порог уже отправляли
        return not is_quiet_hour(msk_hour)
    return True


def format_event(result):
    """Человекочитаемое сообщение по классифицированному событию.

    None — формат не задан, вызывающий использует прежний build_message.
    """
    kind, status, name = result.get("kind"), result.get("status"), result.get("name", "?")
    detail = result.get("detail") or ""
    if kind == KIND_INCIDENT:
        if status == "down":
            return chr(0x1F534) + " " + name + " — нет ответа" + detail
        if status == "up":
            return chr(0x1F7E2) + " " + name + " — работает" + detail
        return None
    if kind == KIND_SSL:
        target = result.get("target") or name
        days = result.get("days")
        if days is None:
            return None
        return (
            chr(0x1F510) + " SSL-сертификат " + str(target)[:120]
            + " истекает через " + str(days) + " дн."
            + " Продлите заранее — после истечения браузеры покажут «сайт небезопасен»."
        )
    if kind == KIND_BACKUP:
        if status == "down":
            return (chr(0x1F7E1) + " Резервная копия не создалась (" + str(name)[:100]
                    + "). Скрипт бэкапа не прислал пульс за отведённое время.")
        if status == "up":
            return chr(0x1F7E2) + " Пульс бэкапа пришёл — резервная копия создана."
        return None
    return None
