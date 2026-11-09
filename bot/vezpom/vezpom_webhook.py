#!/usr/bin/env python3
"""VeZhPom v3.1: Uptime Kuma -> MAX chat + on-demand monitors + subscription.

New in v3.1 (по ТЗ Шефа):
  * уведомления показывают текущее состояние сервера/сайта: время отклика (ping)
    и сообщение от Kuma (например, код ответа HTTP или текст ошибки);
  * команда /check — живая проверка текущего состояния всех мониторов
    пользователя через Kuma API (статус, отклик, сообщение).

Commands in MAX chat: /monitor /backup /link /scan /history /domain /domains /domain_off /check /digest /settings /pay /status /cancel /возврат_средств /rules
"""
import ipaddress
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import db
import kuma
import digest
import domain_watch
import secscan
import urlcheck
import vezlogic
import vitrina_client as vitrina

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID = int(os.environ.get("CHAT_ID", "0"))
API_URL = os.environ.get("API_URL", "https://platform-api2.max.ru")
PORT = int(os.environ.get("PORT", "4321"))
# Секретный путь вебхука: Kuma шлёт на /kuma/<секрет>; пусто — прежнее поведение
# (принимать всё), используется только на переходный период (PR-1).
WEBHOOK_SECRET = os.environ.get("VEZPOM_WEBHOOK_SECRET", "").strip()
# Публичный базовый URL пульсов бэкапов (nginx location /kuma-push/)
PUSH_PUBLIC_URL = os.environ.get("VEZPOM_PUSH_URL", "https://rusalts.ru/kuma-push/")

if not BOT_TOKEN or not CHAT_ID:
    sys.exit("ERROR: BOT_TOKEN and CHAT_ID must be set (see .env.example)")

# Двухшаговый сценарий: chat_id -> режим ожидания адреса
# ('monitor'|'monitor_off'|'domain'|'domain_off'|'link'|'scan').
# Команда без аргумента включает режим; следующее сообщение пользователя
# (обычно адрес из буфера) уходит в соответствующий поток.
awaiting_mode = {}
# chat_id -> True: bot is waiting for the user to type a domain (PR-5)
AWAIT_LOCK = threading.Lock()


def _set_awaiting(chat_id, mode):
    with AWAIT_LOCK:
        awaiting_mode[chat_id] = mode


def _pop_awaiting(chat_id):
    with AWAIT_LOCK:
        return awaiting_mode.pop(chat_id, None)

CHAT_RE = re.compile(r"chat(\d+)")


def _tariff():
    """Цена/триал/период из Админки витрины (источник правды), кэш TTL 60 с.

    Фолбэк при недоступности витрины: 150 ₽ / 14 дн / 30 дн.
    """
    try:
        t = vitrina.get_tariff() or {}
    except Exception:
        t = {}
    price = t.get("price_rub") or 150
    trial = t.get("trial_days") or db.trial_days()
    period = t.get("period_days") or db.subscription_days()
    try:
        return int(price), int(trial), int(period)
    except (TypeError, ValueError):
        return 150, 14, 30


def _tariff_price_text():
    """«150 ₽/мес» для сообщений — цена всегда из Админки витрины."""
    price, _, period = _tariff()
    return "%s ₽/мес" % price if period == 30 else "%s ₽/%d дн." % (price, period)


def _plan_info(chat_id):
    """Тарифный план пользователя: (название, цена-текст, лимит ресурсов).

    Активная подписка → её план (/api/subscription возвращает plan+limit);
    нет подписки/триал/сбой витрины → базовый «Мониторинг». Если витрина
    не отдаёт планы вовсе — прежний лимит 5 (как до гейтинга).
    """
    plans = vitrina.get_plans("vezhpom")
    by_code = {p.get("plan"): p for p in plans}
    sub = vitrina.get_subscription_info(chat_id) or {}
    p = by_code.get((sub.get("plan") or "").strip())
    if p is None:
        p = by_code.get("monitoring") or (plans[0] if plans else None)
    if not p:
        return "Мониторинг", _tariff_price_text(), 5
    price = "%s ₽/мес" % p.get("price_rub") if (p.get("period_days") or 30) == 30 \
        else "%s ₽/%s дн." % (p.get("price_rub"), p.get("period_days"))
    try:
        limit = int(p.get("resource_limit") or 0) or 5
    except (TypeError, ValueError):
        limit = 5
    return p.get("name") or p.get("plan"), price, limit


def _resources_used(chat_id):
    """Ресурсы под контролем: (сайты/серверы, домены, всего). Бэкап не считается."""
    monitors = [m for m in db.list_monitors(chat_id) if m["target"] != "backup"]
    domains = db.list_domains(chat_id)
    return len(monitors), len(domains), len(monitors) + len(domains)


def _plan_limit_reached(chat_id):
    """Лимит ресурсов тарифа исчерпан? Возвращает текст-отказ или None."""
    _, _, total = _resources_used(chat_id)
    plan_name, price_text, limit = _plan_info(chat_id)
    if total < limit:
        return None
    return ("Лимит тарифа «%s» (%s): %d ресурсов — под контролем уже %d"
            " (сайты/серверы + домены).\nВарианты: убрать лишнее"
            " (/monitor_off, /domain_off) или сменить тариф:"
            " https://rusalts.ru/techologis/p/vezhpom/"
            % (plan_name, price_text, limit, total))


def send(chat_id, text):
    url = API_URL + "/messages?chat_id=" + str(chat_id)
    payload = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Authorization": BOT_TOKEN, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except Exception as exc:
        print("[VeZhPom] send error:", exc, flush=True)
        return False


def main_menu_keyboard():
    """Inline-клавиатура MAX: кнопки-команды + ссылка на витрину."""
    return [{
        "type": "inline_keyboard",
        "payload": {
            "buttons": [
                [
                    {"type": "callback", "text": "📡 Мониторинг", "payload": "menu:monitor"},
                    {"type": "callback", "text": "✅ Проверить", "payload": "menu:check"},
                ],
                [
                    {"type": "callback", "text": "💳 Подписка", "payload": "menu:pay"},
                    {"type": "link", "text": "🛍 Витрина", "url": "https://rusalts.ru/techologis/"},
                ],
                [
                    {"type": "callback", "text": "📊 Статус", "payload": "menu:status"},
                    {"type": "callback", "text": "💾 Бэкапы", "payload": "menu:backup"},
                ],
                [
                    {"type": "callback", "text": "🌐 Домены", "payload": "menu:domains"},
                    {"type": "callback", "text": "❌ Отмена", "payload": "menu:cancel"},
                ],
                [
                    {"type": "callback", "text": "📖 Правила", "payload": "menu:rules"},
                    {"type": "callback", "text": "🆘 Помощь", "payload": "menu:help"},
                ],
            ]
        }
    }]


def send_menu(chat_id, text):
    """Отправить сообщение с кнопками меню."""
    url = API_URL + "/messages?chat_id=" + str(chat_id)
    payload = json.dumps({"text": text, "attachments": main_menu_keyboard()}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Authorization": BOT_TOKEN, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except Exception as exc:
        print("[VeZhPom] send_menu error:", exc, flush=True)
        return False


def answer_callback(callback_id, notification=None, message_text=None, with_menu=False):
    """Ответить на нажатие кнопки (закрывает «часики» на кнопке)."""
    body = {}
    if notification:
        body["notification"] = notification
    if message_text is not None:
        msg = {"text": message_text}
        if with_menu:
            msg["attachments"] = main_menu_keyboard()
        body["message"] = msg
    data = json.dumps(body).encode("utf-8") if body else b"{}"
    url = API_URL + "/answers?callback_id=" + str(callback_id)
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Authorization": BOT_TOKEN, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except Exception as exc:
        print("[VeZhPom] answer_callback error:", exc, flush=True)
        return False


def heartbeat_detail(heartbeat):
    """Human-readable current state from Kuma heartbeat: ping + message."""
    heartbeat = heartbeat or {}
    parts = []
    ping = heartbeat.get("ping")
    if ping:
        parts.append("отклик %s мс" % ping)
    msg = heartbeat.get("msg") or ""
    if msg and msg not in ("OK", "Success", "200", "ok"):
        parts.append(str(msg)[:120])
    return " (" + ", ".join(parts) + ")" if parts else ""


def build_message(data):
    """Build notification text from Kuma webhook (v2 format with monitor+heartbeat
    or legacy flat format). Returns (status, name, text).

    ВАЖНО: в сроковых уведомлениях Kuma (certificate expiry) и monitor, и
    heartbeat приходят как null — обращение .get() без guard'а роняло обработчик
    (prod 04.11: SSL-предупреждения пользователей терялись молча).
    """
    monitor = data.get("monitor") or {}
    if "monitor" in data and monitor:
        name = monitor.get("name", "?")
        heartbeat = data.get("heartbeat") or {}
        status = heartbeat.get("status", 1)
        detail = heartbeat_detail(heartbeat)
        if status == 0:
            return "down", name, chr(0x1F534) + " " + name + " — нет ответа" + detail
        return "up", name, chr(0x1F7E2) + " " + name + " — работает" + detail
    status = data.get("status") or ""
    previous = data.get("previous_status") or ""
    name = data.get("name") or "?"
    heartbeat = data.get("heartbeat") or {}
    detail = heartbeat_detail(heartbeat)
    if status == "down":
        return "down", name, chr(0x1F534) + " " + name + " — нет ответа" + detail
    if status == "up" and previous == "down":
        return "up", name, chr(0x1F7E2) + " " + name + " — работает" + detail
    return status, name, chr(0x1F7E1) + " " + name + " — " + str(status) + detail


def owner_chat_id(name, fallback_text=""):
    """Extract chat id from monitor name 'VeZhPom chat<id>: ...'.

    У cert-уведомлений Kuma monitor=null — владелец ищем по тексту msg
    ('[VeZhPom chat<id>: …][url] … will be expired in N days').
    """
    for source in (name, fallback_text):
        m = CHAT_RE.search(source or "")
        if m:
            try:
                return int(m.group(1))
            except (TypeError, ValueError):
                continue
    return CHAT_ID


def _is_active(chat_id):
    """Подписка активна? Витрина — источник правды.

    - Витрина отвечает active → синхронизируем локальную копию (для /status), True.
    - Витрина 404 (нет подписки) → локальная БД (триал / старая оплата).
    - Сбой сети → True (fail-open, не блокируем мониторинг).
    """
    if vitrina.check_subscription(chat_id):
        return True
    return db.is_active(chat_id)


def _record_event(result, chat_id, data):
    """Журнал событий + открытые предупреждения (PR-2). Не влияет на отправку."""
    monitor = data.get("monitor") or {}
    kuma_id = monitor.get("id")
    name = monitor.get("name") or result.get("name") or ""
    target = result.get("target") or monitor.get("url") or ""
    kind, st = result.get("kind"), result.get("status")
    now = int(time.time())
    detail = result.get("detail") or result.get("message") or ""
    try:
        if kind == vezlogic.KIND_INCIDENT:
            if st == "down":
                db.add_event(chat_id, kuma_id, target, kind, "down", detail, started_at=now)
            elif st == "up":
                db.close_open_incident(chat_id, target, now)
                db.add_event(chat_id, kuma_id, target, kind, "up", detail, started_at=now)
        elif kind == vezlogic.KIND_BACKUP:
            if st == "down":
                db.add_event(chat_id, kuma_id, "backup", kind, "down", detail, started_at=now)
                db.upsert_warning(chat_id, "backup", name)
            elif st == "up":
                db.resolve_warning(chat_id, "backup", name)
                db.add_event(chat_id, kuma_id, "backup", kind, "up", detail, started_at=now)
        elif kind == vezlogic.KIND_SSL:
            days = result.get("days")
            target_key = target or name
            db.upsert_warning(chat_id, "ssl", target_key, days=days)
            db.add_event(chat_id, kuma_id, target_key, kind, "info",
                         ("%d дн." % days) if days else "", started_at=now)
    except Exception as exc:
        print("[VeZhPom] record_event error:", exc, flush=True)


def handle_monitor(data):
    """Check subscription before forwarding a monitor event."""
    # PR-1: лог сырого payload — верифицируем формат сроковых событий SSL
    # (certificate expiry) на живых данных; после стабилизации можно убрать.
    try:
        mtype = (data.get("monitor") or {}).get("type", "?")
        print("[VeZhPom] payload type=%s:" % mtype,
              json.dumps(data, ensure_ascii=False)[:600], flush=True)
    except Exception:
        pass
    monitor = data.get("monitor") or {}
    msg_text = data.get("msg") or ""
    if monitor:
        name = monitor.get("name", "")
        chat_id = owner_chat_id(name, fallback_text=msg_text)
        status, _, legacy_message = build_message(data)
        if name:
            db.update_monitor_status(name, status)
    else:
        # сроковые уведомления Kuma (SSL/домены): monitor=null, владелец — из msg
        chat_id = owner_chat_id(msg_text) if msg_text else CHAT_ID
        status, _, legacy_message = build_message(data)
    # PR-2: классификация → журнал/предупреждения → человекочитаемый формат
    result = vezlogic.classify_event(data)
    _record_event(result, chat_id, data)
    message = vezlogic.format_event(result) or legacy_message
    if not _is_active(chat_id):
        user = db.get_user(chat_id)
        if user and not user["notify_sent"]:
            send(chat_id, "ВежПом: бесплатный период закончился. Оплатите подписку %s — команда /pay" % _tariff_price_text())
            db.mark_notify_sent(chat_id)
        return
    # Тихие часы по МСК (фиксированный UTC+3, чтобы не зависеть от TZ сервера)
    msk_hour = time.gmtime(time.time() + 3 * 3600).tm_hour
    if message and vezlogic.should_forward(result, msk_hour):
        send(chat_id, message)


def _create_manual_payment(chat_id):
    """Создать платёж через витрину (единая касса)."""
    try:
        payment = vitrina.create_payment(chat_id, save_payment_method=True)
        if payment.get("confirmation_url"):
            db.upsert_payment(
                payment["payment_id"], chat_id, 150.0,
                status=payment.get("status") or "pending", kind="first",
            )
        return payment.get("confirmation_url", "")
    except Exception as exc:
        send(chat_id, "Не удалось создать платёж: " + str(exc)[:200])
        return ""


def _handle_refund(chat_id, args):
    """Служебная команда (только для владельца): возврат платежа.
    /refund <payment_id> [сумма] — возврат конкретного платежа
    /refund <chat_id> [сумма] — возврат последнего успешного платежа пользователя
    """
    if chat_id != CHAT_ID:
        send(chat_id, "Команда доступна только владельцу сервиса.")
        return
    if not args:
        send(chat_id, "Формат: /refund <payment_id|chat_id> [сумма]")
        return
    target = args[0]
    amount = None
    if len(args) > 1:
        try:
            amount = float(args[1])
        except ValueError:
            send(chat_id, "Сумма должна быть числом, например: /refund " + target + " 50")
            return
    if "-" in target:
        payment = db.get_payment(target)
        if payment is None:
            send(chat_id, "Платёж не найден в журнале: " + target)
            return
        payment_id = payment["payment_id"]
        full_amount = payment["amount"]
    else:
        try:
            target_chat = int(target)
        except ValueError:
            send(chat_id, "Некорректный chat_id: " + target)
            return
        payment = db.last_succeeded_payment(target_chat)
        if payment is None:
            send(chat_id, "Нет успешных платежей для chat_id " + target)
            return
        payment_id = payment["payment_id"]
        full_amount = payment["amount"]
    ref_amount = amount if amount is not None else full_amount
    try:
        result = vitrina.refund_payment(payment_id, ref_amount)
        if result.get("ok"):
            db.upsert_payment(
                payment_id, chat_id, full_amount,
                status="refunded", kind="refund",
            )
            send(chat_id, "Возврат создан: статус %s, сумма %s руб." % (result.get("status", "pending"), ref_amount))
        else:
            send(chat_id, "Ошибка возврата: " + str(result.get("error") or "нет ответа витрины")[:200])
    except Exception as exc:
        send(chat_id, "Ошибка возврата: " + str(exc)[:200])


def _user_refund(chat_id):
    """Пользовательский возврат средств (по правилам оферты)."""
    payment = db.last_succeeded_payment(chat_id)
    if payment is None:
        send(chat_id, "У вас нет оплаченных платежей для возврата.")
        return
    try:
        result = vitrina.refund_payment(payment["payment_id"], payment["amount"])
        if result.get("ok"):
            db.upsert_payment(
                payment["payment_id"], chat_id, payment["amount"],
                status="refunded", kind="refund",
            )
            send(chat_id, "Возврат средств инициирован: %s руб. (статус %s). Подписка действует до конца оплаченного периода. Полные условия: https://rusalts.ru/oferta" % (payment["amount"], result.get("status", "pending")))
        else:
            send(chat_id, "Ошибка возврата: " + str(result.get("error") or "нет ответа витрины")[:200])
    except Exception as exc:
        send(chat_id, "Ошибка возврата: " + str(exc)[:200])


def normalize_target(raw):
    """Normalize user input to an http(s) URL or None if invalid."""
    t = (raw or "").strip()
    if not t:
        return None
    if "://" not in t:
        t = "http://" + t
    parsed = urllib.parse.urlparse(t)
    host = parsed.hostname or ""
    if not host:
        return None
    # IP address (IPv4/IPv6)
    try:
        ipaddress.ip_address(host)
        return t
    except ValueError:
        pass
    # Domain: latin letters, digits, dots, hyphens; must contain a dot
    if not re.match(r"^[a-zA-Z0-9.-]+$", host):
        return None
    if "." not in host:
        return None
    return t


def _add_monitor_flow(chat_id, target):
    if not kuma.available():
        send(chat_id, "Настройка мониторинга временно недоступна (Kuma не настроен на сервере). Попробуйте позже.")
        return
    norm = normalize_target(target)
    if norm is None:
        send(chat_id, "Не похоже на URL или IP. Примеры: https://example.com, example.com, 192.168.1.10")
        return
    # Защита от дублей: если цель уже настроена этим пользователем — не создаём повторно
    # (сравниваем по хосту, чтобы http/https и порт не считались разными целями)
    parsed_norm = urllib.parse.urlparse(norm)
    for m in db.list_monitors(chat_id):
        if urllib.parse.urlparse(m["target"]).hostname == parsed_norm.hostname:
            send(chat_id, "Мониторинг %s уже настроен. Проверить состояние: /check" % m["target"])
            return
    # Гейтинг: лимит ресурсов тарифа (сайты + домены, общий пул)
    denied = _plan_limit_reached(chat_id)
    if denied:
        send(chat_id, denied)
        return
    name = "VeZhPom chat%d: %s" % (chat_id, norm)
    try:
        kuma_id = kuma.add_http_monitor(name, norm)
    except kuma.KumaError as exc:
        send(chat_id, "Не удалось настроить мониторинг: " + str(exc)[:200])
        return
    row_id = db.add_monitor(chat_id, norm, name, kuma_id, kuma_server="alt")
    # Для https-целей сразу включаем контроль срока SSL-сертификата
    # (Kuma предупреждает на порогах 21/14/7 дней, дедуп встроен в Kuma)
    cert_note = ""
    if urllib.parse.urlparse(norm).scheme == "https":
        try:
            cert_id = kuma.add_cert_monitor("VeZhPom chat%d: %s · SSL" % (chat_id, norm), norm)
            db.set_cert_monitor(row_id, cert_id)
            cert_note = "\n🔒 Срок SSL-сертификата тоже под контролем: предупрежу заранее, до пугающей надписи «сайт небезопасен»."
        except kuma.KumaError as exc:
            print("[VeZhPom] cert monitor failed:", exc, flush=True)
    send(chat_id, "✅ Мониторинг настроен: %s. Уведомления о сбоях и восстановлении будут приходить в этот чат. Проверить текущее состояние: /check%s" % (norm, cert_note))


def _send_backup_instructions(chat_id, token):
    """Инструкция по пульсу бэкапа (push-URL через nginx /kuma-push/)."""
    url = PUSH_PUBLIC_URL + token
    send(chat_id,
         "💾 Контроль резервных копий настроен.\n\n"
         "Добавьте последней строкой в скрипт бэкапа (после успешного копирования):\n"
         'curl -fsS "' + url + '?status=up&msg=OK"\n\n'
         "Если пульс не придёт за сутки, пришлю алерт: «Резервная копия не создалась».\n"
         "Отключить: /backup_off")


def _add_backup_flow(chat_id):
    """Команда /backup: push-монитор контроля резервных копий.

    Один push-монитор на пользователя (MVP): повторный вызов возвращает
    существующий push-URL.
    """
    if not kuma.available():
        send(chat_id, "Настройка недоступна: Kuma не настроен на сервере. Попробуйте позже.")
        return
    for m in db.list_monitors(chat_id):
        if m.get("push_token"):
            _send_backup_instructions(chat_id, m["push_token"])
            return
    name = "VeZhPom chat%d: BACKUP" % chat_id
    try:
        kuma_id, token = kuma.add_push_monitor(name, interval=86400)
    except kuma.KumaError as exc:
        print("[VeZhPom] /backup add failed:", exc, flush=True)
        send(chat_id, "😕 Сервис наблюдения не принял настройку контроля бэкапов."
             " Попробуйте ещё раз через пару минут; если повторится — напишите"
             " в поддержку (v.devops@yandex.ru), мы уже видим такие ошибки в журнале.")
        return
    if not token:
        send(chat_id, "Монитор создан, но токен от Kuma не получен. Попробуйте позже или напишите в поддержку.")
        return
    db.add_monitor(chat_id, "backup", name, kuma_id, kuma_server="alt",
                   push_kuma_id=kuma_id, push_token=token)
    _send_backup_instructions(chat_id, token)


def _add_domain_flow(chat_id, raw):
    """Команда /domain: взять домен под контроль срока регистрации (PR-5)."""
    domain = domain_watch.normalize_domain(raw)
    if domain is None:
        send(chat_id, "Не похоже на домен. Примеры: company.ru, example.com, магазин.рф\nФормат: /domain <домен>")
        return
    existing = db.list_domains(chat_id)
    if any(d["domain"] == domain for d in existing):
        send(chat_id, "Домен %s уже под контролем. Список: /domains" % domain)
        return
    # Лимит 5 доменов — утверждён Шефом 04.11 (решения по PR-5); гейтинг
    # по тарифам «Страж»/«Бизнес» отложен до утверждения тарифов §8 карты
    # Гейтинг: общий пул ресурсов тарифа (сайты + домены); fallback — 5
    denied = _plan_limit_reached(chat_id)
    if denied:
        send(chat_id, denied)
        return
    db.add_domain(chat_id, domain)
    try:
        status, expiry = domain_watch.fetch_expiry(domain)
    except Exception as exc:
        print("[VeZhPom] /domain fetch %s: %s" % (domain, exc), flush=True)
        status, expiry = domain_watch.EXPIRY_UNKNOWN, None
    if status == domain_watch.EXPIRY_NOT_FOUND:
        db.upsert_warning(chat_id, "domain", domain, note="not_found")
        send(chat_id, "✅ Домен %s под контролем, но 🟡 он не найден в реестре —"
             " проверьте написание (возможно опечатка) или домен уже освобождён."
             " Проверю снова в ежедневном обходе (около 09:30 МСК)." % domain)
        return
    if expiry is None:
        send(chat_id, "✅ Домен %s под контролем. Срок регистрации пока не удалось узнать"
             " (реестр не ответил или зона без RDAP/WHOIS) — проверю в ежедневном обходе (около 09:30 МСК)." % domain)
        return
    days = domain_watch.sync_warning(chat_id, domain, expiry)
    send(chat_id, "✅ Домен %s под контролем.\n%s\nСообщу заранее: за 30, 14, 7 и 1 день до окончания."
         % (domain, domain_watch.format_domain_status(domain, expiry, days)))


def _remove_domain_flow(chat_id, raw):
    """Команда /domain_off: снять домен с контроля."""
    domain = domain_watch.normalize_domain(raw)
    if domain is None:
        send(chat_id, "Формат: /domain_off <домен>, например: /domain_off company.ru. Список: /domains")
        return
    if db.remove_domain(chat_id, domain):
        db.resolve_warning(chat_id, "domain", domain)
        send(chat_id, "Домен %s снят с контроля. Вернуть: /domain %s" % (domain, domain))
    else:
        send(chat_id, "Домена %s нет в вашем списке. Список: /domains" % domain)


def _domains_text(chat_id):
    """Команда /domains: список доменов и последние известные сроки."""
    rows = db.list_domains(chat_id)
    if not rows:
        return ("Домены под контролем: нет.\nДобавить: /domain <домен>, например: /domain company.ru")
    open_warnings = {w["target"]: w for w in db.list_open_warnings(chat_id)
                     if w["kind"] == vezlogic.KIND_DOMAIN}
    plan_name, price_text, limit = _plan_info(chat_id)
    lines = ["Домены под контролем (%d; тариф «%s» %s — лимит %d ресурсов на сайты+домены):"
             % (len(rows), plan_name, price_text, limit)]
    for row in rows:
        domain = row["domain"]
        warning = open_warnings.get(domain)
        if warning is not None and warning.get("due_ts") is None:
            if warning.get("note") == "not_found":
                lines.append("🟡 " + domain + " — не найден в реестре"
                             " (проверьте написание: /domain_off и /domain заново)")
            else:
                lines.append("🟡 " + domain + " — срок определить не удалось, проверю в обходе")
            continue
        due_iso = row.get("due_date")
        if due_iso:
            expiry = domain_watch._parse_date(due_iso)
            days = domain_watch.days_left(expiry)
            lines.append(domain_watch.format_domain_status(domain, expiry, days))
        else:
            lines.append("🟡 " + domain + " — срок ещё не проверялся (обход около 09:30 МСК)")
    lines.append("Убрать домен: /domain_off <домен>")
    return "\n".join(lines)


def _delete_kuma_monitor(chat_id, name, kuma_id):
    """Удалить монитор из Kuma с проверкой (гонка delete как у add).

    True — монитор удалён (или его там уже не было); False — не удалось.
    """
    if kuma_id:
        try:
            kuma.delete_monitor(kuma_id)
        except Exception as exc:
            print("[VeZhPom] delete monitor %s: %s" % (name[:60], exc), flush=True)
    # гонка ответа: проверяем фактически — монитор ещё в Kuma?
    try:
        if kuma.find_monitor_id_by_name(name):
            time.sleep(1.5)
            if kuma_id:
                try:
                    kuma.delete_monitor(kuma_id)
                except Exception as exc:
                    print("[VeZhPom] delete retry %s: %s" % (name[:60], exc), flush=True)
            if kuma.find_monitor_id_by_name(name):
                return False
    except Exception as exc:
        print("[VeZhPom] delete verify %s: %s" % (name[:60], exc), flush=True)
    return True


def _remove_monitor_flow(chat_id, raw):
    """Команда /monitor_off: снять мониторинг сайта/сервера (вопрос Шефа 04.11).

    Удаляем из Kuma сам монитор и его SSL-контроль, затем строку из БД.
    """
    monitors = [m for m in db.list_monitors(chat_id) if m["target"] != "backup"]
    if not raw or not raw.strip():
        if not monitors:
            send(chat_id, "Мониторинг не настроен. Добавить: /monitor <URL или IP>")
            return
        lines = ["Формат: /monitor_off <URL или IP>. Под контролем:"]
        for m in monitors:
            lines.append(" • " + m["target"])
        lines.append("Бэкапы отключаются отдельно: /backup_off")
        send(chat_id, "\n".join(lines))
        return
    norm = normalize_target(raw)
    if norm is None:
        send(chat_id, "Не похоже на URL или IP. Пример: /monitor_off example.com")
        return
    parsed = urllib.parse.urlparse(norm)
    matches = [m for m in monitors
               if urllib.parse.urlparse(m["target"]).hostname == parsed.hostname]
    if not matches:
        send(chat_id, "Мониторинг %s не найден. Список: /monitor_off" % norm)
        return
    failed = []
    for m in matches:
        ok = True
        for key in ("kuma_id", "cert_kuma_id"):
            if not _delete_kuma_monitor(chat_id, m["monitor_name"] if key == "kuma_id"
                                        else m["monitor_name"] + " · SSL", m.get(key)):
                ok = False
        if ok:
            db.remove_monitor(m["id"])
        else:
            failed.append(m)
    if failed:
        send(chat_id, "⚠️ Не удалось удалить проверку из сервиса наблюдения (сайт %s). "
             "Уведомления по нему могут продолжить приходить — напишите в поддержку"
             " (v.devops@yandex.ru), подчистим вручную." % failed[0]["target"][:80])
        return
    send(chat_id, "✅ Мониторинг %s отключён: уведомления по нему больше не приходят."
         " Вернуть: /monitor %s" % (norm, norm))


def _backup_off_flow(chat_id):
    """Команда /backup_off: отключить контроль резервных копий."""
    rows = [m for m in db.list_monitors(chat_id) if m["target"] == "backup"]
    if not rows:
        send(chat_id, "Контроль бэкапов не настроен. Включить: /backup")
        return
    for m in rows:
        if _delete_kuma_monitor(chat_id, m["monitor_name"], m.get("push_kuma_id")):
            db.remove_monitor(m["id"])
            db.resolve_warning(chat_id, "backup", m["monitor_name"])
        else:
            send(chat_id, "⚠️ Не удалось удалить монитор бэкапов из сервиса наблюдения."
                " Напишите в поддержку (v.devops@yandex.ru), подчистим вручную.")
            return
    send(chat_id, "✅ Контроль резервных копий отключён. Вернуть: /backup")


def _check_monitors(chat_id):
    """Команда /check: живая проверка текущего состояния мониторов из Kuma."""
    monitors = db.list_monitors(chat_id)
    if not monitors:
        send(chat_id, "Мониторы не настроены. Команда /monitor — добавить URL или IP.")
        return
    if not kuma.available():
        send(chat_id, "Проверка состояния недоступна: Kuma API не настроен на сервере (KUMA_USER/KUMA_PASS). /status — статус подписки")
        return
    lines = ["Текущее состояние (%d монитор(ов)):" % len(monitors)]
    for m in monitors:
        try:
            status, ping, msg = kuma.get_monitor_status(m["kuma_id"])
        except Exception as exc:
            print("[VeZhPom] /check status error:", exc, flush=True)
            lines.append("? " + m["target"] + " — нет данных от Kuma")
            continue
        if status == "up":
            icon = chr(0x1F7E2)
            extra = "работает"
        elif status == "down":
            icon = chr(0x1F534)
            extra = "недоступен"
        else:
            icon = chr(0x1F7E1)
            extra = str(status)
        line = "%s %s — %s" % (icon, m["target"], extra)
        if ping:
            line += ", отклик %s мс" % ping
        if msg and msg not in ("OK", "Success", "ok"):
            line += " (%s)" % str(msg)[:120]
        lines.append(line)
    send(chat_id, chr(10).join(lines))


def _link_flow(chat_id, raw):
    """Команда /link: проверить ссылку на фишинг/малварь (Этап 2).

    Лимит 20 проверок в сутки на чат (журнал событий, без новых таблиц).
    """
    used = urlcheck.count_today(chat_id)
    if used >= urlcheck.LINK_DAILY_LIMIT:
        send(chat_id, "Дневной лимит проверок ссылок исчерпан (%d из %d)."
             " Придёт в силу снова завтра утром." % (used, urlcheck.LINK_DAILY_LIMIT))
        return
    verdict, text = urlcheck.check_url(raw)
    if verdict != "unknown" or raw.strip():
        db.add_event(chat_id, None, (raw or "")[:180], "link", "info",
                     verdict, started_at=int(time.time()))
    send(chat_id, text)


def _scan_flow(chat_id, raw):
    """Команда /scan: базовая диагностика безопасности СВОЕГО сайта (Этап 2).

    Лимит 3 проверки в сутки на чат. Не пентест: только то, что видит
    любой посетитель (TLS, заголовки, порты) — по согласию владельца.
    """
    used = secscan.count_today(chat_id)
    if used >= secscan.SCAN_DAILY_LIMIT:
        send(chat_id, "Дневной лимит проверок исчерпан (%d из %d). Придёт в силу"
             " снова завтра утром." % (used, secscan.SCAN_DAILY_LIMIT))
        return
    host = secscan.normalize_host(raw)
    if not host:
        send(chat_id, "Укажите свой сайт или IP. Пример: /scan example.com")
        return
    send(chat_id, "🔎 Проверяю %s (TLS, заголовки, порты — минута)…" % host)
    try:
        level, lines = secscan.scan_host(host)
    except Exception as exc:
        print("[VeZhPom] /scan %s: %s" % (host, exc), flush=True)
        send(chat_id, "🟡 Проверка не удалась — часть сервисов не ответила. Попробуйте позже.")
        return
    db.add_event(chat_id, None, host[:180], "scan", "info", level,
                 started_at=int(time.time()))
    head = {"bad": "🔴 Нашлись серьёзные замечания:",
            "warn": "🟡 Серьёзного нет, но есть что улучшить:",
            "ok": "🟢 Проверка пройдена:"}[level]
    send(chat_id, "Отчёт по %s:\n%s\n%s\nЭто диагностика по согласию владельца,"
         " без вторжения — и не заменяет полноценный аудит."
         % (host, head, "\n".join(" • " + ln for ln in lines)))


def _status_text(chat_id):
    """Статус подписки (источник правды — витрина) + мониторы пользователя."""
    plan_name, price_text, limit = _plan_info(chat_id)
    sites, domains, total = _resources_used(chat_id)
    plan_line = ("Тариф: «%s» (%s) — ресурсов %d из %d (сайты %d + домены %d)."
                 % (plan_name, price_text, total, limit, sites, domains))
    plan_line += "\nВаш ID (для выдачи доступа в админке АЛТ): %s" % chat_id
    if total >= limit:
        plan_line += "\nПредел тарифа — новые сайты/домены не добавить, смените тариф: https://rusalts.ru/techologis/p/vezhpom/"
    sub = vitrina.get_subscription_info(chat_id)
    if sub and sub.get("status") == "active":
        expires = (sub.get("expires_at") or "")[:10]
        days_left = ""
        try:
            from datetime import datetime as _dt
            exp = _dt.fromisoformat(sub["expires_at"])
            days_left = ", осталось %d дн." % max(0, (exp - _dt.utcnow()).days)
        except Exception:
            pass
        status_line = "Подписка активна до %s%s." % (expires, days_left)
    else:
        user = db.get_user(chat_id)
        status_line = "Подписка не активна."
        if user and user["status"] == "trial" and user["paid_until"] > int(time.time()):
            days_left = max(0, (user["paid_until"] - int(time.time())) // 86400)
            status_line = "Пробный период: ещё %d дн. После — %s (/pay)." % (days_left, _tariff_price_text())
        elif user and user["status"] == "canceled":
            status_line = "Автоплатежи отключены. Подписка действует до конца оплаченного периода."
        else:
            status_line += " Оплатить: /pay или кнопка «Подписка» в меню."
    lines = [plan_line, status_line]
    monitors = db.list_monitors(chat_id)
    if monitors:
        lines.append("Мониторы (%d):" % len(monitors))
        for m in monitors:
            if m["last_status"] == "up":
                icon = chr(0x1F7E2)
            elif m["last_status"] == "down":
                icon = chr(0x1F534)
            else:
                icon = chr(0x1F7E1)
            lines.append(icon + " " + m["target"])
        lines.append("Живая проверка состояния: /check")
    else:
        lines.append("Мониторы не настроены. Команда /monitor — добавить URL или IP.")
    return chr(10).join(lines)


def handle_command(text, chat_id):
    cmd = (text or "").strip()
    if not cmd:
        return
    # Голый "/" — показать меню (как подсказку пользователю)
    if cmd == "/":
        send_menu(chat_id, "Меню ВежПом — выберите действие кнопками или введите команду:")
        return
    # Двухшаговый сценарий: не-командное сообщение в режиме ожидания — это адрес
    mode = _pop_awaiting(chat_id)
    if mode and not cmd.startswith("/"):
        flow = {
            "monitor": _add_monitor_flow,
            "monitor_off": _remove_monitor_flow,
            "domain": _add_domain_flow,
            "domain_off": _remove_domain_flow,
            "link": _link_flow,
            "scan": _scan_flow,
        }.get(mode)
        if flow:
            flow(chat_id, cmd)
            return
    low = cmd.lower()
    if low == "/refund" or low.startswith("/refund "):
        _handle_refund(chat_id, cmd.split()[1:])
        return
    if low in ("/link", "/ссылка") or low.startswith("/link ") or low.startswith("/ссылка "):
        arg = cmd.split(None, 1)[1] if len(cmd.split()) > 1 else ""
        if arg:
            _link_flow(chat_id, arg)
        else:
            _set_awaiting(chat_id, "link")
            send(chat_id, "Пришлите ссылку следующим сообщением (обычно она уже в буфере) — проверю на фишинг и вредоносное.")
        return
    if low in ("/scan", "/аудит") or low.startswith("/scan ") or low.startswith("/аудит "):
        arg = cmd.split(None, 1)[1] if len(cmd.split()) > 1 else ""
        if arg:
            _scan_flow(chat_id, arg)
        else:
            _set_awaiting(chat_id, "scan")
            send(chat_id, "Пришлите адрес СВОЕГО сайта или IP следующим сообщением (обычно он в буфере) — сделаю диагностику безопасности.")
        return
    if low == "/monitor_off" or low.startswith("/monitor_off ") or low in ("/отключить_мониторинг",) or low.startswith("/отключить_мониторинг "):
        arg = cmd.split(None, 1)[1] if len(cmd.split()) > 1 else ""
        if arg:
            _remove_monitor_flow(chat_id, arg)
        else:
            monitors = [m for m in db.list_monitors(chat_id) if m["target"] != "backup"]
            lines = ["Под контролем:"]
            for m in monitors:
                lines.append(" • " + m["target"])
            lines.append("Пришлите адрес того, что снять с контроля (обычно он в буфере).")
            _set_awaiting(chat_id, "monitor_off")
            send(chat_id, "\n".join(lines))
        return
    if low == "/backup_off" or low in ("/бэкап_выкл",):
        _backup_off_flow(chat_id)
        return
    if low in ("/monitor", "/monitor_on", "/настройка", "/настройка_мониторинга"):
        _set_awaiting(chat_id, "monitor")
        send(chat_id, "Введите URL сайта или IP сервера для мониторинга. Примеры: https://example.com, example.com, 192.168.1.10\n\nДля https-сайтов сразу включу и контроль срока SSL-сертификата.")
        return
    if low == "/backup":
        _add_backup_flow(chat_id)
        return
    if low in ("/domain", "/домен") or low.startswith("/domain ") or low.startswith("/домен "):
        parts = cmd.split()
        if len(parts) > 1:
            _add_domain_flow(chat_id, " ".join(parts[1:]))
        else:
            _set_awaiting(chat_id, "domain")
            send(chat_id, "Введите домен, чтобы я следил за сроком его регистрации. Примеры: company.ru, магазин.рф\n\nПредупрежу заранее: за 30, 14, 7 и 1 день. Список: /domains")
        return
    if low in ("/domains", "/домены"):
        send(chat_id, _domains_text(chat_id))
        return
    if low in ("/domain_off", "/домен_выкл") or low.startswith("/domain_off ") or low.startswith("/домен_выкл "):
        parts = cmd.split()
        if len(parts) > 1:
            _remove_domain_flow(chat_id, " ".join(parts[1:]))
        else:
            domains = db.list_domains(chat_id)
            lines = ["Домены под контролем:"]
            for d in domains:
                lines.append(" • " + d["domain"])
            lines.append("Пришлите домен, который снять с контроля (обычно он в буфере).")
            _set_awaiting(chat_id, "domain_off")
            send(chat_id, "\n".join(lines))
        return
    if low == "/digest":
        text = digest.build_traffic_light(chat_id)
        send(chat_id, text or "Мониторы не настроены — /monitor и /backup добавят наблюдение.")
        return
    if low in ("/history", "/журнал"):
        text = digest.build_report(chat_id, days=30, title="📋 Журнал за 30 дней:")
        send(chat_id, text)
        return
    if low in ("/settings", "/настройки"):
        p = db.get_prefs(chat_id)
        send(chat_id,
             "⚙️ Настройки оповещений:\n"
             "• Светофор по утрам: %s (в %s МСК)\n"
             "• Еженедельный отчёт: %s (Пн %s МСК)\n\n"
             "Переключить: /settings light on|off, /settings weekly on|off"
             % ("вкл" if p["traffic_light"] else "выкл", p["light_time"],
                "вкл" if p["weekly_report"] else "выкл", p["weekly_time"]))
        return
    if low.startswith("/settings "):
        parts = cmd.split()
        if len(parts) == 3 and parts[1] in ("light", "weekly") and parts[2] in ("on", "off"):
            value = 1 if parts[2] == "on" else 0
            if parts[1] == "light":
                db.set_prefs(chat_id, traffic_light=value)
            else:
                db.set_prefs(chat_id, weekly_report=value)
            send(chat_id, "Готово: %s — %s." % ("светофор" if parts[1] == "light" else "еженедельный отчёт",
                                                "вкл" if value else "выкл"))
        else:
            send(chat_id, "Формат: /settings light on|off, /settings weekly on|off")
        return
    if low == "/check":
        _check_monitors(chat_id)
        return
    if low == "/pay":
        payment_url = _create_manual_payment(chat_id)
        if payment_url:
            send(chat_id, "Ссылка на оплату %s: " % _tariff_price_text() + payment_url)
            send(chat_id, "После оплаты нажмите «Статус» — подписка активируется автоматически.")
        return
    if low == "/status":
        send(chat_id, _status_text(chat_id))
        return
    if low == "/cancel":
        try:
            vitrina.delete_payment_method(chat_id)
        except Exception as exc:
            send(chat_id, "Не удалось отвязать карту: " + str(exc)[:200])
            return
        db.cancel_subscription(chat_id)
        send(chat_id, "Автоплатежи отключены, карта отвязана. Подписка действует до конца оплаченного периода.")
        return
    if low in ("/возврат", "/возврат_средств", "/refundme"):
        _user_refund(chat_id)
        return
    if low in ("/start", "/help"):
        send_menu(chat_id, "ВежПом — мониторинг и охрана ваших сайтов и серверов.\n\nВыберите действие кнопками ниже или введите команду:\n/monitor — настроить мониторинг (URL или IP, https → и SSL-сроки)\n/backup — контроль резервных копий (пульс из скрипта бэкапа)\n/domain — следить за сроком домена (30/14/7/1 день)\n/domains — список доменов\n/domain_off <домен> — убрать домен из контроля\n/check — текущее состояние\n/link <ссылка> — проверить на фишинг\n/history — журнал за 30 дней\n/scan <сайт> — диагностика безопасности\n/scan <сайт> — диагностика безопасности\n/monitor_off — отключить мониторинг сайта\n/backup_off — отключить контроль бэкапов\n/digest — светофор прямо сейчас\n/history — журнал за 30 дней\n/settings — светофор и еженедельный отчёт: вкл/выкл\n/pay — оплатить подписку\n/status — статус и мониторы\n/cancel — отписка\n/возврат_средств — возврат\n/rules — правила")
        return
    if low in ("/menu", "/меню"):
        send_menu(chat_id, "Меню ВежПом — выберите действие:")
        return
    if low in ("/rules", "/offer", "/oferta"):
        price, trial, _period = _tariff()
        send(chat_id, "ВежПом: подписка %s, пробный период %d дн. Возврат в течение 14 дней с оплаты (полный или пропорциональный), если услуга не оказывалась. Отключить автоплатежи и отвязать карту: /cancel. Полный текст оферты и политики возвратов: https://rusalts.ru/oferta" % (_tariff_price_text(), trial))
        return
    # Любой другой не-командный текст (приветствие, вопрос и т.п.) — показать меню с кнопками.
    # Важно: событие bot_started приходит только при ПЕРВОМ запуске бота, у существующих
    # пользователей (начали раньше) его не будет — поэтому меню показываем на любой текст.
    if not cmd.startswith("/"):
        send_menu(chat_id, "👋 Я **ВежПом** — мониторинг работоспособности серверов и сайтов.\n\n💡 Просто напишите любое сообщение — меню появится снова.\nВыберите действие кнопками ниже или введите команду:\n/monitor — настроить мониторинг (URL или IP)\n/backup — контроль резервных копий\n/domain — следить за сроком домена\n/domains — список доменов\n/check — текущее состояние\n/link <ссылка> — проверить на фишинг\n/pay — оплатить подписку\n/status — статус и мониторы\n/cancel — отписка\n/возврат_средств — возврат\n/rules — правила")
        return
    # Неизвестная команда
    send(chat_id, "Неизвестная команда. /menu — меню с кнопками, /help — список команд")


def handle_callback(chat_id, callback_id, payload):
    """Обработка нажатия кнопок меню (message_callback)."""
    action = (payload or "").lower()
    if action == "menu:monitor":
        _set_awaiting(chat_id, "monitor")
        answer_callback(
            callback_id,
            notification="Введите URL или IP",
            message_text="Введите URL сайта или IP сервера для мониторинга. Примеры: https://example.com, example.com, 192.168.1.10",
        )
    elif action == "menu:check":
        answer_callback(callback_id, notification="Проверяю…")
        _check_monitors(chat_id)
    elif action == "menu:backup":
        answer_callback(callback_id, notification="Бэкапы")
        _add_backup_flow(chat_id)
    elif action == "menu:domains":
        answer_callback(callback_id, notification="Домены")
        send(chat_id,
             "🌐 Контроль доменов — напомню о продлении заранее (за 30, 14, 7 и 1 день):\n"
             "/domain <домен> — взять под контроль\n"
             "/domains — список доменов и сроки\n"
             "/domain_off <домен> — убрать из контроля")
    elif action == "menu:pay":
        answer_callback(callback_id, notification="Оплата")
        handle_command("/pay", chat_id)
    elif action == "menu:status":
        answer_callback(callback_id, notification="Статус")
        handle_command("/status", chat_id)
    elif action == "menu:cancel":
        answer_callback(callback_id, notification="Отмена подписки")
        handle_command("/cancel", chat_id)
    elif action == "menu:rules":
        answer_callback(callback_id, notification="Правила")
        handle_command("/rules", chat_id)
    elif action == "menu:help":
        answer_callback(callback_id, notification="Помощь")
        handle_command("/help", chat_id)
    else:
        answer_callback(callback_id, notification="Неизвестная команда")


def handle_bot_started(chat_id):
    """Новый пользователь открыл/запустил бота — приветствие с меню."""
    price_text = _tariff_price_text()
    trial_days = _tariff()[1]
    send_menu(
        chat_id,
        "👋 Здравствуйте! Я **ВежПом** — мониторинг работоспособности серверов и сайтов.\n\n"
        "💡 Просто напишите любое сообщение — меню с кнопками появится снова.\n\n"
        "📡 Мониторинг — настроить наблюдение за сайтом или сервером\n"
        "✅ Проверить — текущее состояние\n"
        "💳 Подписка — %s (пробный период %d дн.)\n" % (price_text, trial_days)
        + "🛍 Витрина — все продукты АЛТ (rusalts.ru/techologis/)\n"
        "📊 Статус — подписка и мониторы\n"
        "📖 Правила — оферта и возвраты",
    )


def handle_yookassa_webhook(data):
    """Устаревший вебхук ЮKassa: платежи теперь проходят через Витрину
    (единая касса rusalts.ru). Оставлен для совместимости со старыми
    настройками — просто логируем и не трогаем подписку."""
    print("[VeZhPom] legacy /yookassa webhook ignored (payments via Vitrina)", flush=True)


def poll_commands():
    """Long polling: read user commands from MAX and process them."""
    marker = None
    while True:
        try:
            url = API_URL + "/updates?limit=5&timeout=25"
            if marker is not None:
                url += "&marker=" + str(marker)
            req = urllib.request.Request(url)
            req.add_header("Authorization", BOT_TOKEN)
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            marker = data.get("marker") or marker
            for update in data.get("updates") or []:
                ut = update.get("update_type")
                if ut == "message_created":
                    msg = update.get("message") or {}
                    body = msg.get("body") or {}
                    text = body.get("text", "") if isinstance(body, dict) else ""
                    recipient = msg.get("recipient") or {}
                    chat_id = recipient.get("chat_id")
                    if chat_id is None:
                        continue
                    if text:
                        print("[VeZhPom] command from chat", chat_id, ":", text, flush=True)
                        handle_command(text, chat_id)
                elif ut == "message_callback":
                    cb = update.get("callback") or {}
                    callback_id = cb.get("callback_id")
                    payload = cb.get("payload", "")
                    msg = update.get("message") or {}
                    recipient = msg.get("recipient") or {}
                    chat_id = recipient.get("chat_id")
                    if chat_id is None or callback_id is None:
                        continue
                    print("[VeZhPom] callback from chat", chat_id, ":", payload, flush=True)
                    handle_callback(chat_id, callback_id, payload)
                elif ut == "bot_started":
                    chat_id = update.get("chat_id")
                    if chat_id is not None:
                        print("[VeZhPom] bot_started from chat", chat_id, flush=True)
                        handle_bot_started(chat_id)
        except Exception as exc:
            print("[VeZhPom] poll error:", exc, flush=True)
        time.sleep(2)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length))
            path = self.path.split("?")[0].rstrip("/")
            if path == "/yookassa":
                handle_yookassa_webhook(data)
            elif not WEBHOOK_SECRET or path == "/kuma/" + WEBHOOK_SECRET:
                handle_monitor(data)
            else:
                # секрет настроен, путь не совпал — не отдаём содержимое чужим запросам
                print("[VeZhPom] webhook rejected (bad path):", path[:80], flush=True)
                self.send_response(403)
                self.end_headers()
                return
        except Exception as exc:
            print("[VeZhPom] handler error:", exc, flush=True)
        self.send_response(200)
        self.end_headers()

    def log_message(self, fmt, *args):
        return


if __name__ == "__main__":
    db.connect()
    threading.Thread(target=poll_commands, daemon=True).start()
    digest.start(send)  # светофор 09:00 МСК и еженедельный отчёт Пн 10:00 (PR-3)
    domain_watch.start(send)  # суточный обход доменов 09:30 МСК (PR-5)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("[VeZhPom v3.3] listening on port", PORT, flush=True)
    server.serve_forever()
