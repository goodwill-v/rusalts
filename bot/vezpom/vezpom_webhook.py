#!/usr/bin/env python3
"""VeZhPom v3.1: Uptime Kuma -> MAX chat + on-demand monitors + subscription.

New in v3.1 (по ТЗ Шефа):
  * уведомления показывают текущее состояние сервера/сайта: время отклика (ping)
    и сообщение от Kuma (например, код ответа HTTP или текст ошибки);
  * команда /check — живая проверка текущего состояния всех мониторов
    пользователя через Kuma API (статус, отклик, сообщение).

Commands in MAX chat: /monitor /check /pay /status /cancel /возврат_средств /rules
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
import vitrina_client as vitrina

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID = int(os.environ.get("CHAT_ID", "0"))
API_URL = os.environ.get("API_URL", "https://platform-api2.max.ru")
PORT = int(os.environ.get("PORT", "4321"))

if not BOT_TOKEN or not CHAT_ID:
    sys.exit("ERROR: BOT_TOKEN and CHAT_ID must be set (see .env.example)")

# chat_id -> True: bot is waiting for the user to type a target URL/IP
awaiting_target = {}
AWAIT_LOCK = threading.Lock()

CHAT_RE = re.compile(r"chat(\d+)")


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
    or legacy flat format). Returns (status, name, text)."""
    if "monitor" in data:
        name = data.get("monitor", {}).get("name", "?")
        heartbeat = data.get("heartbeat", {}) or {}
        status = heartbeat.get("status", 1)
        detail = heartbeat_detail(heartbeat)
        if status == 0:
            return "down", name, chr(0x1F534) + " " + name + " — нет ответа" + detail
        return "up", name, chr(0x1F7E2) + " " + name + " — работает" + detail
    status = data.get("status", "")
    previous = data.get("previous_status", "")
    name = data.get("name", "?")
    heartbeat = data.get("heartbeat", {}) or {}
    detail = heartbeat_detail(heartbeat)
    if status == "down":
        return "down", name, chr(0x1F534) + " " + name + " — нет ответа" + detail
    if status == "up" and previous == "down":
        return "up", name, chr(0x1F7E2) + " " + name + " — работает" + detail
    return status, name, chr(0x1F7E1) + " " + name + " — " + str(status) + detail


def owner_chat_id(name):
    """Extract chat id from monitor name 'VeZhPom chat<id>: ...'."""
    m = CHAT_RE.search(name or "")
    if m:
        try:
            return int(m.group(1))
        except (TypeError, ValueError):
            return CHAT_ID
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


def handle_monitor(data):
    """Check subscription before forwarding a monitor event."""
    if "monitor" in data:
        name = data.get("monitor", {}).get("name", "")
        chat_id = owner_chat_id(name)
        status, _, message = build_message(data)
        db.update_monitor_status(name, status)
    else:
        chat_id = CHAT_ID
        status, _, message = build_message(data)
    if not _is_active(chat_id):
        user = db.get_user(chat_id)
        if user and not user["notify_sent"]:
            send(chat_id, "ВежПом: бесплатный период закончился. Оплатите подписку 150 руб/мес — команда /pay")
            db.mark_notify_sent(chat_id)
        return
    if message:
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
    name = "VeZhPom chat%d: %s" % (chat_id, norm)
    try:
        kuma_id = kuma.add_http_monitor(name, norm)
    except kuma.KumaError as exc:
        send(chat_id, "Не удалось настроить мониторинг: " + str(exc)[:200])
        return
    db.add_monitor(chat_id, norm, name, kuma_id, kuma_server="alt")
    send(chat_id, "✅ Мониторинг настроен: %s. Уведомления о сбоях и восстановлении будут приходить в этот чат. Проверить текущее состояние: /check" % norm)


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


def _status_text(chat_id):
    """Статус подписки (источник правды — витрина) + мониторы пользователя."""
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
            status_line = "Пробный период: ещё %d дн. После — 150 руб/мес (/pay)." % days_left
        elif user and user["status"] == "canceled":
            status_line = "Автоплатежи отключены. Подписка действует до конца оплаченного периода."
        else:
            status_line += " Оплатить: /pay или кнопка «Подписка» в меню."
    lines = [status_line]
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
    # Режим ожидания цели мониторинга: любое не-командное сообщение — это URL/IP
    with AWAIT_LOCK:
        waiting = awaiting_target.get(chat_id, False)
    if waiting and not cmd.startswith("/"):
        with AWAIT_LOCK:
            awaiting_target[chat_id] = False
        _add_monitor_flow(chat_id, cmd)
        return
    low = cmd.lower()
    if low == "/refund" or low.startswith("/refund "):
        _handle_refund(chat_id, cmd.split()[1:])
        return
    if low in ("/monitor", "/monitor_on", "/настройка", "/настройка_мониторинга"):
        with AWAIT_LOCK:
            awaiting_target[chat_id] = True
        send(chat_id, "Введите URL сайта или IP сервера для мониторинга. Примеры: https://example.com, example.com, 192.168.1.10")
        return
    if low == "/check":
        _check_monitors(chat_id)
        return
    if low == "/pay":
        payment_url = _create_manual_payment(chat_id)
        if payment_url:
            send(chat_id, "Ссылка на оплату 150 руб/мес: " + payment_url)
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
        send_menu(chat_id, "ВежПом — мониторинг работоспособности серверов и сайтов.\n\nВыберите действие кнопками ниже или введите команду:\n/monitor — настроить мониторинг (URL или IP)\n/check — текущее состояние\n/pay — оплатить подписку\n/status — статус и мониторы\n/cancel — отписка\n/возврат_средств — возврат\n/rules — правила")
        return
    if low in ("/menu", "/меню"):
        send_menu(chat_id, "Меню ВежПом — выберите действие:")
        return
    if low in ("/rules", "/offer", "/oferta"):
        send(chat_id, "ВежПом: подписка 150 руб/мес, пробный период 14 дней. Возврат в течение 14 дней с оплаты (полный или пропорциональный), если услуга не оказывалась. Отключить автоплатежи и отвязать карту: /cancel. Полный текст оферты и политики возвратов: https://rusalts.ru/oferta")
        return
    # Любой другой не-командный текст (приветствие, вопрос и т.п.) — показать меню с кнопками.
    # Важно: событие bot_started приходит только при ПЕРВОМ запуске бота, у существующих
    # пользователей (начали раньше) его не будет — поэтому меню показываем на любой текст.
    if not cmd.startswith("/"):
        send_menu(chat_id, "👋 Я **ВежПом** — мониторинг работоспособности серверов и сайтов.\n\n💡 Просто напишите любое сообщение — меню появится снова.\nВыберите действие кнопками ниже или введите команду:\n/monitor — настроить мониторинг (URL или IP)\n/check — текущее состояние\n/pay — оплатить подписку\n/status — статус и мониторы\n/cancel — отписка\n/возврат_средств — возврат\n/rules — правила")
        return
    # Неизвестная команда
    send(chat_id, "Неизвестная команда. /menu — меню с кнопками, /help — список команд")


def handle_callback(chat_id, callback_id, payload):
    """Обработка нажатия кнопок меню (message_callback)."""
    action = (payload or "").lower()
    if action == "menu:monitor":
        with AWAIT_LOCK:
            awaiting_target[chat_id] = True
        answer_callback(
            callback_id,
            notification="Введите URL или IP",
            message_text="Введите URL сайта или IP сервера для мониторинга. Примеры: https://example.com, example.com, 192.168.1.10",
        )
    elif action == "menu:check":
        answer_callback(callback_id, notification="Проверяю…")
        _check_monitors(chat_id)
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
    send_menu(
        chat_id,
        "👋 Здравствуйте! Я **ВежПом** — мониторинг работоспособности серверов и сайтов.\n\n"
        "💡 Просто напишите любое сообщение — меню с кнопками появится снова.\n\n"
        "📡 Мониторинг — настроить наблюдение за сайтом или сервером\n"
        "✅ Проверить — текущее состояние\n"
        "💳 Подписка — 150 ₽/мес (пробный период 14 дней)\n"
        "🛍 Витрина — все продукты АЛТ (rusalts.ru/techologis/)\n"
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
            if self.path == "/yookassa":
                handle_yookassa_webhook(data)
            else:
                handle_monitor(data)
        except Exception as exc:
            print("[VeZhPom] handler error:", exc, flush=True)
        self.send_response(200)
        self.end_headers()

    def log_message(self, fmt, *args):
        return


if __name__ == "__main__":
    db.connect()
    threading.Thread(target=poll_commands, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("[VeZhPom v3.1] listening on port", PORT, flush=True)
    server.serve_forever()
