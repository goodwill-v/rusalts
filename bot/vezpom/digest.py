#!/usr/bin/env python3
"""digest.py — ежедневный «светофор» и еженедельный отчёт ВежПома (PR-3).

Светофор (по умолчанию 09:00 МСК) — одно сообщение: что в норме, какие
предупреждения, какие проблемы. Еженедельный отчёт (Пн 10:00 МСК) — сбои,
простой, рекомендации человеческим языком. Планировщик — фоновый поток
рядом с poll_commands; время фиксированно МСК (UTC+3), чтобы не зависеть
от TZ сервера.
"""
import threading
import time

import db

MSK_OFFSET = 3 * 3600
WARN_LINES_LIMIT = 3  # максимум предупреждений/проблем в одном светофоре


def msk_now():
    """Текущее время МСК (сервер может иметь любой TZ)."""
    return time.gmtime(time.time() + MSK_OFFSET)


def _days_left(due_ts):
    if not due_ts:
        return None
    return max(0, (due_ts - int(time.time()) + 86399) // 86400)


def _fmt_minutes(seconds):
    h, m = seconds // 3600, (seconds % 3600) // 60
    return ("%d ч %d мин" % (h, m)) if h else ("%d мин" % m)


def build_traffic_light(chat_id):
    """Собрать светофор одним сообщением. None — нечего присылать.

    🟢 всё в норме; 🔴 сервисы не отвечают; 🟡 предупреждения (SSL, бэкапы, домены).
    """
    monitors = db.list_monitors(chat_id)
    services = [m for m in monitors if m["target"] != "backup"]
    backups = [m for m in monitors if m["target"] == "backup"]
    warnings = db.list_open_warnings(chat_id)
    down = [m for m in services if m.get("last_status") == "down"]

    if not monitors and not warnings:
        return None  # пользователь ещё ничего не настроил — не шумим

    lines = []
    if down:
        lines.append(chr(0x1F534) + " Проблемы (%d):" % len(down))
        for m in down[:WARN_LINES_LIMIT]:
            lines.append(" • " + m["target"] + " — нет ответа")
        if len(down) > WARN_LINES_LIMIT:
            lines.append(" • …и ещё %d" % (len(down) - WARN_LINES_LIMIT))
    if warnings:
        lines.append(chr(0x1F7E1) + " Предупреждения (%d):" % len(warnings))
        for w in warnings[:WARN_LINES_LIMIT]:
            days = _days_left(w.get("due_ts"))
            label = str(w["target"])
            if w["kind"] == "ssl":
                label += " — SSL истекает через %d дн." % days if days is not None else " — SSL: срок неизвестен"
            elif w["kind"] == "backup":
                label = "Резервная копия не создалась (" + label + ")"
            elif w["kind"] == "domain":
                if w.get("note") == "not_found":
                    label += " — не найден в реестре (проверьте написание)"
                elif days is None:
                    label += " — срок домена не удалось определить"
                elif days <= 0:
                    label += " — срок регистрации истёк, продлите срочно"
                else:
                    label += " — домен продлевается через %d дн." % days
            lines.append(" • " + label[:140])
        if len(warnings) > WARN_LINES_LIMIT:
            lines.append(" • …и ещё %d" % (len(warnings) - WARN_LINES_LIMIT))
    if not lines:
        tail = (", бэкап под контролем" if backups else "")
        lines.append(chr(0x1F7E2) + " Всё в норме: %d сервис(ов)%s. Я на посту."
                     % (len(services), tail))
    return "\n".join(lines)


def build_weekly_report(chat_id):
    """Еженедельный отчёт: сбои, простой, топ ресурса, рекомендации."""
    return build_report(chat_id, days=7, title="📊 Отчёт за неделю:")


def build_report(chat_id, days=7, title="📊 Отчёт за неделю:"):
    """Отчёт по журналу за период (дней): сбои, простой, разбивка по ресурсам.

    days=7 — еженедельный отчёт (с рекомендациями); days=30 — журнал за
    месяц (/history): та же статистика + разбивка по каждому ресурсу.
    """
    now = int(time.time())
    events = db.list_events(chat_id, since_ts=now - days * 86400)
    downs = [e for e in events if e["kind"] == "incident" and e["status"] == "down"]
    warns = db.list_open_warnings(chat_id)

    lines = [title]
    if not downs and not warns:
        lines.append(chr(0x1F7E2) + " Сбоев не было, всё спокойно.")
        return "\n".join(lines)

    downtime = 0
    open_downs = 0
    per_target = {}  # target -> [раз, секунд]
    for e in downs:
        if e.get("resolved_at"):
            dur = max(0, e["resolved_at"] - e["started_at"])
        else:
            dur = max(0, now - e["started_at"])
            open_downs += 1
        downtime += dur
        acc = per_target.setdefault(e["target"] or "?", [0, 0])
        acc[0] += 1
        acc[1] += dur

    lines.append("• Сбоев: %d, суммарный простой: %s" % (len(downs), _fmt_minutes(downtime)))
    if per_target:
        top_target, (top_cnt, top_dur) = max(per_target.items(), key=lambda kv: kv[1][1])
        lines.append("• Больше всех волновал %s (%d раз, %s)"
                     % (top_target[:80], top_cnt, _fmt_minutes(top_dur)))
    if open_downs:
        lines.append("• " + chr(0x1F534) + " Сейчас не отвечает: %d" % open_downs)
    if warns:
        lines.append("• Незакрытых предупреждений: %d" % len(warns))
    # Для журнала за месяц — разбивка по каждому ресурсу (вместо рекомендаций)
    if days > 7 and per_target:
        lines.append("Разбивка по ресурсам:")
        for target, (cnt, dur) in sorted(per_target.items(), key=lambda kv: -kv[1][1])[:8]:
            lines.append(" • %s — %d раз, %s" % (target[:80], cnt, _fmt_minutes(dur)))
        if len(per_target) > 8:
            lines.append(" • …и ещё %d" % (len(per_target) - 8))
    if days <= 7:
        recs = []
        if any(w["kind"] == "ssl" for w in warns):
            recs.append("продлить SSL-сертификаты")
        if any(w["kind"] == "domain" for w in warns):
            recs.append("продлить домены (без них сайт и почта встанут)")
        if any(w["kind"] == "backup" for w in warns):
            recs.append("проверить скрипт резервного копирования")
        if max((v[1] for v in per_target.values()), default=0) > 3600:
            recs.append("обсудить с хостингом частые сбои")
        if recs:
            lines.append("• Рекомендация: " + "; ".join(recs) + ".")
    return "\n".join(lines)


# ---- Планировщик ----

def _parse_hhmm(value, default="09:00"):
    try:
        hh, mm = str(value).split(":")
        return int(hh) % 24, int(mm) % 60
    except Exception:
        hh, mm = default.split(":")
        return int(hh), int(mm)


def _tick(send_fn):
    """Один проход расписания по всем активным пользователям."""
    now = msk_now()
    ts = time.time()
    msk_hm = (now.tm_hour, now.tm_min)
    day_id = int((ts + MSK_OFFSET) // 86400)
    week_id = int((ts + MSK_OFFSET - now.tm_wday * 86400 - now.tm_hour * 3600
                   - now.tm_min * 60) // 604800)
    for chat_id in db.list_active_users():
        prefs = db.get_prefs(chat_id)
        hh, mm = _parse_hhmm(prefs["light_time"])
        if prefs["traffic_light"] and msk_hm >= (hh, mm) \
                and day_id != prefs["last_light_sent"]:
            text = build_traffic_light(chat_id)
            if text:
                send_fn(chat_id, text)
            db.mark_digest_sent(chat_id, "last_light_sent", day_id)
        hh, mm = _parse_hhmm(prefs["weekly_time"], "10:00")
        if prefs["weekly_report"] and now.tm_wday == prefs["weekly_day"] \
                and msk_hm >= (hh, mm) and week_id != prefs["last_weekly_sent"]:
            text = build_weekly_report(chat_id)
            if text:
                send_fn(chat_id, text)
            db.mark_digest_sent(chat_id, "last_weekly_sent", week_id)


def run_scheduler(send_fn, check_interval=30):
    """Фоновый цикл планировщика (поток-демон)."""
    while True:
        try:
            _tick(send_fn)
        except Exception as exc:
            print("[VeZhPom] scheduler error:", exc, flush=True)
        time.sleep(check_interval)


def start(send_fn):
    """Запустить планировщик рядом с poll_commands."""
    threading.Thread(target=run_scheduler, args=(send_fn,), daemon=True).start()
