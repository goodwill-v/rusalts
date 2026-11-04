#!/usr/bin/env python3
"""domain_watch.py — контроль сроков регистрации доменов (PR-5).

Uptime Kuma 1.23.17 домены не умеет (появились в 2.x), поэтому модуль свой.
Один запрос на домен в сутки — вежливо к реестрам.

Как узнаём срок:
  * RDAP по HTTPS через rdap.org (IANA-бутстрап: сам редиректит на сервер
    зоны, например .com → rdap.verisign.com). Срок — events[].eventDate
    с eventAction == "expiration".
  * Зоны .ru / .su / .рф в RDAP-бутстрапе отсутствуют (проверено 04.11:
    rdap.org отвечает 404, rdap.tcinet.ru не существует) — для них WHOIS
    на whois.tcinet.ru, поле paid-till. Стандартная библиотека (socket),
    без новых зависимостей.
  * Остальным зонам без RDAP — попытка через whois.iana.org (refer →
    сервер зоны, поля Registry Expiry Date / paid-till / …).
  * Не удалось определить срок — 🟡-предупреждение «срок неизвестен»,
    не краш: увидит пользователь в светофоре и одним сообщением.

Анти-шум — как у SSL (§4 дорожной карты): пороги 30/14/7/1 дней, одно
сообщение на порог (дедуп через warnings UNIQUE chat+kind+target и
last_level = отправленный порог), тихие часы 23:00–08:00 МСК через
vezlogic.is_quiet_hour — порог, попавший в ночь, откладывается до утра
(обход идёт в 09:30 МСК, сразу после светофора).

Планировщик — отдельный поток-демон рядом с планировщиком светофора
(digest.start), идемпотентность через sweep_state (один проход в МСК-сутки).
"""
import json
import re
import socket
import threading
import time
import urllib.request
from datetime import date, datetime

import db
import vezlogic

RDAP_BASE = "https://rdap.org/domain/"
WHOIS_RU_SERVER = "whois.tcinet.ru"
IANA_WHOIS = "whois.iana.org"
# Зоны без публичного RDAP: смотрим сразу WHOIS Координационного центра
WHOIS_RU_ZONES = ("ru", "su", "рф", "xn--p1ai")
REQUEST_TIMEOUT = 20
USER_AGENT = "VeZhPom domain-watch (+https://rusalts.ru)"

# Суточный обход: 09:30 МСК (после светофора 09:00 — предупреждения
# появляются сразу после него и попадут в завтрашний светофор всегда)
SWEEP_NAME = "domains"
SWEEP_AT = (9, 30)   # МСК
SWEEP_PAUSE = 1.0    # сек между запросами к реестрам (вежливые лимиты)
MSK_OFFSET = 3 * 3600

# Временный лимит доменов на пользователя (❓РАЗВИЛКА: подтвердить у Шефа;
# тарифы §8 дорожной карты ещё не утверждены — гейтинг отложен)
MAX_DOMAINS_PER_USER = 5

DOMAIN_LABEL_RE = re.compile(r"^[a-z0-9а-яё-]+$", re.IGNORECASE)
TLD_RE = re.compile(r"^[a-zа-яё]{2,}$|^xn--[a-z0-9-]+$")

# Ключи срока в WHOIS-ответах (порядок = приоритет; "free-date" НЕ срок оплаты)
WHOIS_EXPIRY_KEYS = (
    "paid-till",                # .ru/.su/.рф (tcinet)
    "registry expiry date",     # ICANN-формат
    "expiry date",
    "expiration date",
    "expiration time",
    "expire",
    "expires",
    "expires on",
)


# ---- Чистые функции: разбор данных и решение о пороге ----

def _parse_date(value):
    """Строка даты/времени → datetime.date. None — не разобрать.

    Понимает ISO с Z/офсетом ("2027-04-01T17:40:36Z"), дату без времени
    ("2027-04-01") и точечные форматы WHOIS ("01.04.2027").
    """
    if not value:
        return None
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y.%m.%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def parse_rdap_expiry(data):
    """RDAP-ответ (dict) → date окончания регистрации или None.

    Стандарт RFC 7483: события в поле events, срок — eventAction "expiration"
    (некоторые реестры пишут с заглавной или с припиской — нормализуем).
    """
    if not isinstance(data, dict):
        return None
    events = data.get("events")
    if not isinstance(events, list):
        return None
    for ev in events:
        if not isinstance(ev, dict):
            continue
        action = str(ev.get("eventAction") or "").strip().lower()
        if action == "expiration":
            parsed = _parse_date(ev.get("eventDate"))
            if parsed:
                return parsed
    return None


def parse_whois_expiry(text):
    """WHOIS-ответ (текст) → date окончания регистрации или None.

    Ищем известные ключи срока (paid-till у .ru, Registry Expiry Date у
    ICANN-реестров). free-date (дата освобождения после грейс-периода)
    не подходит — пользователь должен продлить ДО paid-till.
    """
    if not text:
        return None
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        k = str(key).strip().lower()
        if k in WHOIS_EXPIRY_KEYS:
            parsed = _parse_date(val)
            if parsed:
                return parsed
    return None


def days_left(expiry, today=None):
    """Целых дней до окончания (>= 0; уже истёкшие — 0). None — нет даты."""
    if expiry is None:
        return None
    t = today if today is not None else date.today()
    return max(0, (expiry - t).days)


def classify_domain(days):
    """Дней до окончания → {'level': порог, 'days': days} или None.

    Пороги как у SSL (vezlogic.DOMAIN_THRESHOLDS = 30/14/7/1): level —
    ближайший пройденный порог. None — срок далеко (> 30 дн.): без
    мгновенного сообщения и без 🟡 в светофоре (§4 карты).
    """
    if days is None:
        return None
    for t in sorted(vezlogic.DOMAIN_THRESHOLDS):
        if days <= t:
            return {"level": t, "days": days}
    return None


def normalize_domain(raw):
    """Ввод пользователя → домен нижним регистром, или None.

    Терпимо к вставке URL (https://www.x.ru/path), завершающей точке и
    регистру; IDN (магазин.рф) остаётся кириллицей для показа, ASCII-форму
    делает to_ascii_domain при запросе.
    """
    t = (raw or "").strip()
    if not t:
        return None
    if "://" in t:
        t = t.split("://", 1)[1]
    t = t.split("/", 1)[0].split("?", 1)[0].split(":", 1)[0]
    t = t.strip(".").lower()
    if t.startswith("www."):
        t = t[4:]
    if "." not in t or len(t) > 253:
        return None
    labels = t.split(".")
    for label in labels:
        if not label or len(label) > 63 or not DOMAIN_LABEL_RE.match(label) \
                or label.startswith("-") or label.endswith("-"):
            return None
    if not TLD_RE.match(labels[-1]):
        return None  # TLD должен быть буквенным (числовые — это IP)
    try:
        to_ascii_domain(t)
    except Exception:
        return None
    return t


def to_ascii_domain(domain):
    """Кириллический домен → punycode (мир.рф → xn--h1ahn.xn--p1ai) для RDAP/WHOIS."""
    d = (domain or "").strip().strip(".").lower()
    if d.isascii():
        return d
    try:
        return d.encode("idna").decode("ascii").lower()
    except UnicodeError:
        # IDNA-кодек строг к некоторым валидным меткам — кодируем по частям
        parts = []
        for label in d.split("."):
            if label.isascii():
                parts.append(label)
            else:
                parts.append(label.encode("idna").decode("ascii"))
        return ".".join(parts).lower()


# ---- Сеть: RDAP / WHOIS (None/"" = не удалось — это 🟡-событие, не крах) ----

def _fetch_rdap(ascii_domain, timeout=REQUEST_TIMEOUT):
    """JSON RDAP-ответа через rdap.org (следует редиректу на сервер зоны)."""
    req = urllib.request.Request(
        RDAP_BASE + ascii_domain,
        headers={"Accept": "application/rdap+json, application/json",
                 "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:
        print("[VeZhPom] RDAP %s: %s" % (ascii_domain, exc), flush=True)
        return None


def _fetch_whois(server, query, timeout=REQUEST_TIMEOUT):
    """Сырой текст WHOIS (TCP:43, стандартная библиотека)."""
    try:
        with socket.create_connection((server, 43), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall((query + "\r\n").encode("utf-8"))
            chunks = []
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks).decode("utf-8", "replace")
    except Exception as exc:
        print("[VeZhPom] WHOIS %s %s: %s" % (server, query, exc), flush=True)
        return ""


def _iana_whois_server(tld, timeout=REQUEST_TIMEOUT):
    """Сервер WHOIS зоны из ответа whois.iana.org (строка 'refer: ...')."""
    text = _fetch_whois(IANA_WHOIS, tld, timeout)
    for line in text.splitlines():
        if line.lower().startswith("refer:"):
            server = line.split(":", 1)[1].strip()
            if server:
                return server
    return None


def fetch_expiry(domain, timeout=REQUEST_TIMEOUT):
    """Дата окончания регистрации домена или None (🟡 «срок неизвестен»).

    .ru/.su/.рф → WHOIS whois.tcinet.ru (paid-till); остальные — RDAP
    rdap.org, при неудаче — WHOIS через refer от whois.iana.org.
    """
    ascii_d = to_ascii_domain(domain)
    tld = ascii_d.rsplit(".", 1)[-1]
    if tld in WHOIS_RU_ZONES:
        text = _fetch_whois(WHOIS_RU_SERVER, ascii_d, timeout)
        return parse_whois_expiry(text) if text else None
    data = _fetch_rdap(ascii_d, timeout)
    expiry = parse_rdap_expiry(data) if data else None
    if expiry:
        return expiry
    # RDAP не поддерживает зону или реестр не ответил — последний шанс WHOIS
    server = _iana_whois_server(tld, timeout)
    if server:
        text = _fetch_whois(server, ascii_d, timeout)
        return parse_whois_expiry(text) if text else None
    return None


# ---- Сообщения (стиль ВежПома: простой язык) ----

def _plural_days(n):
    if n % 100 in (11, 12, 13, 14):
        return "дней"
    if n % 10 == 1:
        return "день"
    if n % 10 in (2, 3, 4):
        return "дня"
    return "дней"


def format_domain_warning(domain, days, expiry=None):
    """Мгновенное сообщение о пороге (одно на порог, стиль как у SSL)."""
    if days <= 0:
        return ("🟡 Срок регистрации домена " + domain + " истёк. "
                "Без продления сайт и почта перестанут работать — продлите срочно у регистратора.")
    msg = ("🟡 Домен " + domain + " истекает через " + str(days) + " " + _plural_days(days)
           + " — продлевайте заранее, без домена сайт и почта перестанут работать.")
    if expiry is not None:
        msg += "\nРегистрация оплачена до " + expiry.strftime("%d.%m.%Y") + "."
    return msg


def format_domain_status(domain, expiry, days):
    """Короткий статус для /domain и /domains."""
    if expiry is None:
        return "🟡 " + domain + " — срок определить не удалось (реестр не ответил или зона без RDAP/WHOIS)"
    if days <= 0:
        return "🔴 " + domain + " — срок регистрации истёк, продлите срочно"
    if days <= max(vezlogic.DOMAIN_THRESHOLDS):
        return "🟡 " + domain + " — оплачен до " + expiry.strftime("%d.%m.%Y") \
            + ", осталось " + str(days) + " " + _plural_days(days)
    return "🟢 " + domain + " — оплачен до " + expiry.strftime("%d.%m.%Y") \
        + ", ещё " + str(days) + " " + _plural_days(days)


# ---- Анти-шум: журнал + предупреждения + решение о сообщении ----

def sync_warning(chat_id, domain, expiry):
    """Обновить предупреждение по известной дате (без отправки сообщений).

    Держим due_ts актуальным всегда: прыжок срока вперёд > 60 сут (продление)
    сбрасывает last_level в 99 — пороги нового цикла снова активны.
    Если срок далеко (> 30 дн.) — предупреждение закрываем (в светофоре шума
    не будет), но из-за актуального due_ts продление всё равно распознается.
    """
    days = days_left(expiry)
    db.upsert_warning(chat_id, "domain", domain, days=days)
    if classify_domain(days) is None:
        db.resolve_warning(chat_id, "domain", domain)
    return days


def process_expiry(chat_id, domain, expiry, send_fn, msk_hour, today=None):
    """Один результат проверки → журнал, предупреждение, возможно сообщение.

    Возвращает действие: ok (срок далеко) | silent (порог уже отправляли или
    между порогами) | deferred (тихие часы — порог отложен до утра, ничего
    не пишем, чтобы утренний обход смог отправить) | notified (отправлено) |
    failed (срок неизвестен).

    Дедуп порогов: warnings.last_level хранит ОТПРАВЛЕННЫЙ порог (99 = ещё
    не шликали). В db.upsert_warning при первой вставке last_level=days —
    это семантика SSL-вебхуков PR-2, поэтому для доменов читаем предыдущее
    значение ДО upsert и сами решаем по уровню порога.
    """
    now = int(time.time())
    if expiry is None:
        prev = db.get_warning(chat_id, "domain", domain)
        db.upsert_warning(chat_id, "domain", domain)  # due_ts=None → 🟡 «срок неизвестен»
        db.add_event(chat_id, None, domain, vezlogic.KIND_DOMAIN, "info",
                     "не удалось определить срок", started_at=now)
        if prev is None or not prev["open"]:
            send_fn(chat_id, "🟡 Не удалось узнать срок домена " + domain
                    + " — реестр не ответил или зона без RDAP/WHOIS. Проверю снова в ежедневном обходе.")
        return "failed"
    db.set_domain_due(chat_id, domain, expiry.isoformat())
    days = days_left(expiry, today)
    info = classify_domain(days)
    if info is None:
        sync_warning(chat_id, domain, expiry)
        return "ok"
    level = info["level"]
    prev = db.get_warning(chat_id, "domain", domain)
    last = prev["last_level"] if prev and prev["last_level"] is not None else 99
    if level >= last:
        # этот (или более ранний) порог уже отправляли — держим due_ts актуальным
        db.upsert_warning(chat_id, "domain", domain, days=days)
        return "silent"
    if vezlogic.is_quiet_hour(msk_hour):
        # тихие часы 23:00–08:00 МСК (§4 карты): не создаём и не помечаем —
        # утренний обход (09:30) увидит порог и отправит сообщение
        return "deferred"
    db.upsert_warning(chat_id, "domain", domain, days=days)
    send_fn(chat_id, format_domain_warning(domain, days, expiry))
    db.mark_warning_notified(chat_id, "domain", domain, level)
    db.add_event(chat_id, None, domain, vezlogic.KIND_DOMAIN, "info",
                 "%d дн." % days, started_at=now)
    return "notified"


# ---- Суточный обход ----

def check_domain(chat_id, domain, send_fn, today=None, msk_hour=None):
    """Проверить один домен и разрулить анти-шум. Возвращает действие."""
    if msk_hour is None:
        msk_hour = time.gmtime(time.time() + MSK_OFFSET).tm_hour
    try:
        expiry = fetch_expiry(domain)
    except Exception as exc:
        print("[VeZhPom] domain check %s: %s" % (domain, exc), flush=True)
        expiry = None
    return process_expiry(chat_id, domain, expiry, send_fn, msk_hour, today=today)


def check_all(send_fn, pause=SWEEP_PAUSE, today=None, msk_hour=None):
    """Суточный обход всех доменов активных пользователей (вежливая пауза между запросами)."""
    targets = db.list_active_domains()
    stats = {"checked": 0, "notified": 0, "failed": 0}
    for i, (chat_id, domain) in enumerate(targets):
        if i and pause:
            time.sleep(pause)
        try:
            action = check_domain(chat_id, domain, send_fn, today=today, msk_hour=msk_hour)
        except Exception as exc:
            print("[VeZhPom] domain sweep %s: %s" % (domain, exc), flush=True)
            action = "failed"
        stats["checked"] += 1
        if action == "notified":
            stats["notified"] += 1
        elif action == "failed":
            stats["failed"] += 1
    return stats


# ---- Планировщик (по образцу digest.run_scheduler, идемпотентность PR-3) ----

def _sweep_tick(send_fn, now_ts=None):
    ts = time.time() if now_ts is None else now_ts
    shifted = ts + MSK_OFFSET
    day_id = int(shifted // 86400)
    if day_id == db.get_sweep_day(SWEEP_NAME):
        return  # сегодня уже обходили (переживает рестарт, как prefs.last_light_sent)
    gmt = time.gmtime(shifted)
    if (gmt.tm_hour, gmt.tm_min) < SWEEP_AT:
        return  # до 09:30 МСК — рано
    stats = check_all(send_fn)
    db.mark_sweep_sent(SWEEP_NAME, day_id)
    print("[VeZhPom] domain sweep done:", stats, flush=True)


def run_scheduler(send_fn, check_interval=60):
    while True:
        try:
            _sweep_tick(send_fn)
        except Exception as exc:
            print("[VeZhPom] domain scheduler error:", exc, flush=True)
        time.sleep(check_interval)


def start(send_fn):
    """Запустить суточный обход доменов рядом с планировщиком светофора."""
    threading.Thread(target=run_scheduler, args=(send_fn,), daemon=True).start()
