#!/usr/bin/env python3
"""secscan.py — базовая диагностика безопасности по согласию владельца.

НЕ пентест и не вторжение: только то, что видит любой посетитель.
Пользователь сам запросил /scan <host> — это согласие на проверку СВОЕГО
ресурса; в отчёте напоминаем об этом.

Что проверяем:
  * TLS-сертификат: срок, проходит ли проверку цепочки (self-signed/кривые);
  * HTTP: редирект http→https, security-заголовки (HSTS, X-Frame-Options,
    X-Content-Type-Options, CSP), версия сервера в баннере;
  * «опасные» порты: FTP/Telnet/SMB/RDP/VNC/Redis/MySQL/PostgreSQL открыты
    наружу — частая дыра.

Лимит — 3 проверки в сутки на чат (журнал событий, kind='scan').
"""
import socket
import ssl
import time
import urllib.parse

HTTP_TIMEOUT = 10
PORT_TIMEOUT = 1.5

# Порт → (название, зачем закрывать)
RISKY_PORTS = {
    21: "FTP", 23: "Telnet", 445: "SMB (шары Windows)",
    3306: "MySQL", 5432: "PostgreSQL", 5900: "VNC",
    6379: "Redis (часто без пароля!)", 3389: "RDP",
}

SECURITY_HEADERS = {
    "strict-transport-security": "HSTS (принуждение к https)",
    "x-frame-options": "защита от встраивания во фреймы",
    "x-content-type-options": "защита от подмены типа ответа",
    "content-security-policy": "защита от XSS (CSP)",
}


def normalize_host(raw):
    """Ввод пользователя → хост без схемы/пути, или None."""
    t = (raw or "").strip()
    if not t:
        return None
    if "://" in t:
        t = t.split("://", 1)[1]
    t = t.split("/", 1)[0].split("?", 1)[0].split(":", 1)[0].strip(".").lower()
    if not t or " " in t or "." not in t and not t.replace(".", "").isdigit():
        return None
    if len(t) > 253:
        return None
    return t


def _tls_check(host):
    """Сертификат на 443: (найден ли, дней_до_конца, issuer, цепочка_ок)."""
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((host, 443), timeout=HTTP_TIMEOUT) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                cert = tls.getpeercert()
                issuer = ""
                for rdn in cert.get("issuer") or ():
                    for k, v in rdn:
                        if k == "organizationName":
                            issuer = v
                expires = ssl.cert_time_to_seconds(cert["notAfter"])
                days = int((expires - time.time()) // 86400)
                return True, days, issuer, True, None
    except ssl.SSLCertVerificationError as exc:
        # сертификат есть, но цепочка/имя не проходят — это серьёзный знак
        detail = "имя не совпадает с доменом" if "hostname mismatch" in str(exc) \
            else "цепочка не проходит проверку (self-signed?)"
        return True, None, "", False, detail
    except Exception:
        return False, None, "", None, None


def _http_headers(host):
    """Заголовки ответа https://host/ (dict, lower-ключи) или None."""
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=HTTP_TIMEOUT) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                tls.sendall(("GET / HTTP/1.1\r\nHost: %s\r\n"
                             "User-Agent: VeZhPom-scan\r\nConnection: close\r\n\r\n"
                             % host).encode())
                chunks = []
                while True:
                    chunk = tls.recv(4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    if sum(len(c) for c in chunks) > 65536:
                        break
        head = b"".join(chunks).split(b"\r\n\r\n", 1)[0].decode("latin-1", "replace")
        lines = head.splitlines()
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, _, v = line.partition(":")
                headers[k.strip().lower()] = v.strip()
        return headers
    except Exception:
        return None


def _http_redirects_to_https(host):
    """Отвечает ли http:// редиректом на https (грубая проверка статус+Location)."""
    try:
        with socket.create_connection((host, 80), timeout=HTTP_TIMEOUT) as sock:
            sock.sendall(("GET / HTTP/1.1\r\nHost: %s\r\n"
                          "User-Agent: VeZhPom-scan\r\nConnection: close\r\n\r\n" % host).encode())
            data = b""
            while len(data) < 8192:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        head = data.split(b"\r\n\r\n", 1)[0].decode("latin-1", "replace")
        first = head.splitlines()[0] if head else ""
        return ("301" in first or "302" in first or "308" in first) and "https" in head.lower()
    except Exception:
        return None


def _open_risky_ports(host):
    """Список открытых «опасных» портов [(порт, название)]."""
    open_ports = []
    for port, label in RISKY_PORTS.items():
        try:
            with socket.create_connection((host, port), timeout=PORT_TIMEOUT):
                open_ports.append((port, label))
        except Exception:
            pass
    return open_ports


def scan_host(host):
    """Полная проверка. Возвращает (level, lines).

    level: bad (есть 🔴) | warn (есть 🟡) | ok | unknown (сайт не отвечает).
    """
    lines = []
    has_bad = has_warn = False

    # TLS
    found, days, issuer, chain_ok, chain_detail = _tls_check(host)
    if found and chain_ok:
        text = "🟢 Сертификат TLS действителен (%s)" % (issuer or "издатель не назван")
        if days is not None:
            text += ", до истечения %d дн." % days
            if days <= 14:
                lines.append("🟡 " + text.replace("🟢 ", "") + " — пора продлевать")
                has_warn = True
            else:
                lines.append(text)
        else:
            lines.append(text)
    elif found and not chain_ok:
        lines.append("🔴 Сертификат TLS не проходит проверку (%s) — браузеры пугают"
                     " посетителей «сайт небезопасен»" % chain_detail)
        has_bad = True
    else:
        lines.append("🔴 TLS/HTTPS на 443 не отвечает — шифрования нет")
        has_bad = True

    # Заголовки
    headers = _http_headers(host)
    if headers is not None:
        server = headers.get("server", "")
        if server and any(ch.isdigit() for ch in server):
            lines.append("🟡 Сервер показывает версию: «%s» — скроите её в настройках"
                         " (злоумышленнику это подсказка)" % server[:60])
            has_warn = True
        missing = [label for key, label in SECURITY_HEADERS.items()
                   if key not in headers]
        if missing:
            lines.append("🟡 Нет заголовков защиты: %s — добавьте в настройки"
                         " веб-сервера (шаблоны легко гуглятся)" % ", ".join(missing))
            has_warn = True
        else:
            lines.append("🟢 Все базовые security-заголовки на месте")
    else:
        lines.append("🔴 https:// не отвечает на запрос (сайт недоступен снаружи?)")
        has_bad = True

    # http→https
    redir = _http_redirects_to_https(host)
    if redir is False:
        lines.append("🟡 http:// не перенаправляет на https:// — посетители могут"
                     " остаться на незащищённой версии")
        has_warn = True
    elif redir is True:
        lines.append("🟢 http:// правильно переводит на https://")

    # Порты
    open_ports = _open_risky_ports(host)
    for port, label in open_ports:
        lines.append("🔴 Открыт порт %d (%s) наружу — закройте фаерволом,"
                     " если он не нужен извне" % (port, label))
        has_bad = True
    if not open_ports:
        lines.append("🟢 Опасные порты (FTP/Telnet/SMB/RDP/VNC/Redis/БД) закрыты")

    level = "bad" if has_bad else ("warn" if has_warn else "ok")
    return level, lines


# ---- Лимит проверок в сутки ----

SCAN_DAILY_LIMIT = 3


def count_today(chat_id, now=None):
    """Сколько /scan пользователь сделал сегодня (МСК-сутки)."""
    import db
    ts = time.time() if now is None else now
    day_id = int((ts + 3 * 3600) // 86400)
    day_start = day_id * 86400 - 3 * 3600
    events = db.list_events(chat_id, since_ts=day_start)
    return sum(1 for e in events if e["kind"] == "scan")
