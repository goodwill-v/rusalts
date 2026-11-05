#!/usr/bin/env python3
"""urlcheck.py — проверка подозрительных ссылок (Этап 2, дорожная карта §6).

MVP без ключей и без инфраструктуры:
  * база угроз URLhaus (abuse.ch, открыта и бесплатна) — известные ссылки
    на малварь/фишинг;
  * эвристики обмана: IP вместо домена, punycode (хn--), логин:пароль@ в URL,
    подозрительные TLD (.zip/.top/…), «хвост»-путь с длинным набором символов.
Формулировки честные: «в базах угроз не найдено» ≠ «безопасно».
Чужие сервисы получают только сам проверяемый URL — больше ничего.
"""
import json
import os
import re
import time
import urllib.parse
import urllib.request

URLHAUS_API = "https://urlhaus-api.abuse.ch/v1/url/"
REQUEST_TIMEOUT = 12
USER_AGENT = "VeZhPom link-check (+https://rusalts.ru)"
# abuse.ch требует Auth-Key (бесплатная регистрация); без ключа база
# недоступна — работаем только по эвристикам и честно говорим об этом
URLHAUS_AUTH_KEY = os.environ.get("URLHAUS_AUTH_KEY", "").strip()

# TLD, массово используемые в фишинге (частичный, расширяемый список)
SUSPICIOUS_TLDS = {"zip", "mov", "tk", "top", "xyz", "gq", "cf", "ml", "work",
                   "click", "country", "download", "review"}

# Категории вредоносных записей URLhaus → человеческое название
THREAT_NAMES = {
    "malware_download": "раздача вредоносных файлов",
    "malware": "вредоносная ссылка",
    "phishing": "фишинг",
}


def normalize_url(raw):
    """Ввод пользователя → http(s)://… или None (IP/домен принимаются)."""
    t = (raw or "").strip().strip(".")
    if not t or " " in t:
        return None
    if "://" not in t:
        t = "https://" + t
    parsed = urllib.parse.urlparse(t)
    host = (parsed.hostname or "").lower()
    if not host or "." not in host and not _is_ip(host):
        return None
    if not re.match(r"^[a-z0-9а-яё.\-]+$", host.replace("xn--", "")) and not _is_ip(host):
        return None
    # ВАЖНО: для поиска по базе оставляем netloc С портом (hostname порт
    # теряет, а вредоносные ссылки часто на нестандартных портах)
    netloc = (parsed.netloc or "").lower()
    return urllib.parse.urlunparse((parsed.scheme, netloc, parsed.path or "/",
                                    parsed.params, parsed.query, ""))


def _is_ip(host):
    parts = host.split(".")
    return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


def heuristic_reasons(url):
    """Признаки обмана в самой ссылке (без сети). [] — ничего подозрительного."""
    reasons = []
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "http":
        reasons.append("без шифрования (http://) — данные видны по пути")
    if _is_ip(host):
        reasons.append("вместо имени сайта — голевой IP-адрес")
    if host.startswith("xn--") or ".xn--" in host:
        reasons.append("punycode-домен (xn--…) — так маскируют похожие буквы")
    if "@" in (parsed.netloc or ""):
        reasons.append("логин:пароль@ перед адресом — классика обмана")
    elif "@" in url.lower().split(parsed.scheme + "://", 1)[-1].split("/", 1)[0] or "@" in url:
        reasons.append("знак @ внутри адреса — так скрывают настоящий сайт")
    tld = host.rsplit(".", 1)[-1].lower()
    if tld in SUSPICIOUS_TLDS:
        reasons.append("зона .%s массово используется во фишинге" % tld)
    elif not _is_ip(host):
        low = url.lower()
        hit = next((t for t in SUSPICIOUS_TLDS if ("." + t) in low), None)
        if hit:
            reasons.append("в адресе мелькает зона .%s — часто подменяют такой ссылкой" % hit)
    if len(url) > 120 and url.count("/") >= 5:
        reasons.append("очень длинный «хвост» адреса — часто прячут подмену")
    if host.count("-") >= 3:
        reasons.append("много дефисов в домене — типично для подделок брендов")
    return reasons


def _urlhaus_lookup(url, timeout=REQUEST_TIMEOUT):
    """База URLhaus: (threat_или_None, база_ответила_True/False).

    False — нет ключа или сеть не ответила: база НЕ проверялась.
    """
    if not URLHAUS_AUTH_KEY:
        return None, False
    try:
        data = urllib.parse.urlencode({"url": url}).encode()
        req = urllib.request.Request(
            URLHAUS_API, data=data,
            headers={"User-Agent": USER_AGENT, "Auth-Key": URLHAUS_AUTH_KEY,
                     "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:
        print("[VeZhPom] urlhaus %s: %s" % (url[:80], exc), flush=True)
        return None, False
    if result.get("query_status") == "ok":
        threat = result.get("threat") or "malware"
        return THREAT_NAMES.get(threat, threat), True
    return None, True  # no_threat_found и прочие статусы — база ответила


def check_url(url):
    """Проверить ссылку. Возвращает (verdict, text).

    verdict: danger (в базе угроз) | suspicious (признаки обмана) |
             clean (база ответила — не нашла, признаков нет) |
             unknown (база недоступна — только эвристики).
    """
    url = normalize_url(url)
    if not url:
        return "unknown", "Не похоже на ссылку. Пример: /link https://example.com/pay"
    threat, available = _urlhaus_lookup(url)
    reasons = heuristic_reasons(url)
    base_note = "" if available else \
        "\n(База угроз сейчас недоступна — проверено только по признакам ссылки.)"
    if threat:
        text = "🔴 Осторожно: эта ссылка в базе угроз (%s).\nНе открывайте и никому не пересылайте." % threat
        if reasons:
            text += "\nПризнаки: " + "; ".join(reasons) + "."
        return "danger", text
    if reasons:
        text = ("🟡 Подозрительные признаки:\n• " + "\n• ".join(reasons)
                + "\n\nВходить по ней в свои аккаунты не стоит." + base_note)
        return "suspicious", text
    if not available:
        return ("unknown", "🟡 Признаков обмана в ссылке не видно, но база угроз"
                " не проверялась" + base_note.replace("\n", " ")
                + "\nПароли и коды из сообщений по ссылкам не вводите.")
    return "clean", ("🟢 В базах угроз ссылки нет, признаков обмана не видно."
                     "\nЭто проверка по открытым базам — не гарантия:"
                     " пароли и коды из сообщений по ссылкам не вводите.")


# ---- Лимит проверок в сутки (журнал событий, без новых таблиц) ----

LINK_DAILY_LIMIT = 20


def count_today(chat_id, now=None):
    """Сколько проверок ссылок пользователь сделал сегодня (МСК-сутки)."""
    import db
    ts = time.time() if now is None else now
    day_id = int((ts + 3 * 3600) // 86400)
    day_start = day_id * 86400 - 3 * 3600
    events = db.list_events(chat_id, since_ts=day_start)
    return sum(1 for e in events if e["kind"] == "link")
