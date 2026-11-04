#!/usr/bin/env python3
"""Uptime Kuma client for VeZhPom (create monitors on demand).

Uptime Kuma has no official REST API; the web UI talks Socket.IO.
This module wraps the Socket.IO protocol used by the uptime-kuma
frontend (Kuma 1.23.x):

  * login                       — auth
  * add                         — create monitor (returns monitorID)
  * deleteMonitor               — delete monitor
  * getMonitorList              — emits push event "monitorList" {id: monitorJSON}
  * getMonitorBeats(id, hours)  — heartbeat history (callback data)

Requires: pip install python-socketio
Config (env): KUMA_URL, KUMA_USER, KUMA_PASS, KUMA_NOTIFY_ID
"""
import json
import os
import time

KUMA_URL = os.environ.get("KUMA_URL", "http://127.0.0.1:3001")
KUMA_USER = os.environ.get("KUMA_USER", "")
KUMA_PASS = os.environ.get("KUMA_PASS", "")
# id уведомления в Kuma, которое шлёт вебхук в VeZhPom (в Kuma: "VeZhPom - MAX")
KUMA_NOTIFY_ID = int(os.environ.get("KUMA_NOTIFY_ID", "2"))

try:
    import socketio
except ImportError:
    socketio = None


def available():
    """True if python-socketio is installed and Kuma credentials are set."""
    return socketio is not None and bool(KUMA_USER) and bool(KUMA_PASS)


class KumaError(RuntimeError):
    pass


def _connect():
    if socketio is None:
        raise KumaError("python-socketio не установлен на сервере (pip install python-socketio)")
    if not KUMA_USER or not KUMA_PASS:
        raise KumaError("KUMA_USER/KUMA_PASS не заданы в /opt/vezpom/.env")
    sio = socketio.Client(reconnection=False)
    try:
        sio.connect(KUMA_URL, transports=["websocket"], wait_timeout=15)
    except Exception as exc:
        raise KumaError("Kuma недоступен: %s" % str(exc)[:120])
    return sio


def _login(sio):
    result = {}

    def cb(resp):
        result["resp"] = resp

    try:
        sio.emit("login", {"username": KUMA_USER, "password": KUMA_PASS}, callback=cb)
    except Exception as exc:
        sio.disconnect()
        raise KumaError("login emit error: %s" % str(exc)[:120])
    sio.sleep(2)
    resp = result.get("resp") or {}
    if not resp.get("ok"):
        sio.disconnect()
        raise KumaError("login failed: %s" % json.dumps(resp, ensure_ascii=False)[:200])


def _emit(sio, event, payload, timeout=3):
    result = {}

    def cb(resp):
        result["resp"] = resp

    sio.emit(event, payload, callback=cb)
    sio.sleep(timeout)
    resp = result.get("resp")
    if resp is None:
        sio.disconnect()
        raise KumaError("no response for " + event)
    return resp


def _notify_sparse():
    # Kuma хранит связи как sparse-массив: for...in по notificationIDList даёт
    # ИНДЕКСЫ, поэтому элемент должен стоять на позиции, равной id уведомления:
    # [null, null, 2] привязывает уведомление с id=2 (а не [2]!)
    notify_sparse = [None] * (KUMA_NOTIFY_ID + 1)
    notify_sparse[KUMA_NOTIFY_ID] = KUMA_NOTIFY_ID
    return notify_sparse


def find_monitor_id_by_name(name):
    """Найти монитор по точному имени. None — нет (или Kuma недоступен)."""
    try:
        for m in get_monitors():
            if m.get("name") == name and m.get("id"):
                return int(m["id"])
    except Exception as exc:
        print("[VeZhPom] find monitor by name: %s" % exc, flush=True)
    return None


def add_monitor(payload):
    """Create any monitor in Uptime Kuma. Returns kuma monitor id.

    payload должен содержать type/name/interval и др.; notificationIDList
    (вебхук VeZhPom - MAX) привязывается автоматически.

    Гонка Socket.IO (prod 04.11): «add» доходит до Kuma, монитор создаётся,
    но ответ не успевает за таймаутом — клиент видит «no response for add».
    Лечится самопроверкой: ищем созданный монитор по имени и возвращаем его id
    (иначе бот теряет монитор из вида, а повторная попытка плодит дубли).
    """
    payload = dict(payload)
    name = payload.get("name", "")
    # привязываем вебхук-уведомление VeZhPom, чтобы события доходили до бота
    payload["notificationIDList"] = _notify_sparse()
    sio = _connect()
    try:
        _login(sio)
        resp = _emit(sio, "add", payload, timeout=8)
        # Uptime Kuma отвечает: {"ok": true, "msg": "Added Successfully.", "monitorID": 20}
        monitor_id = resp.get("monitorID")
        if not resp.get("ok") or not monitor_id:
            raise KumaError("add monitor failed: %s" % json.dumps(resp, ensure_ascii=False)[:200])
        return int(monitor_id)
    except KumaError as exc:
        if "no response" in str(exc) and name:
            time.sleep(1.5)
            monitor_id = find_monitor_id_by_name(name)
            if monitor_id:
                print("[VeZhPom] add race: no response, but monitor exists (id %s): %s"
                      % (monitor_id, name[:60]), flush=True)
                return monitor_id
        raise
    finally:
        try:
            sio.disconnect()
        except Exception:
            pass


def add_http_monitor(name, url, interval=60):
    """Create an HTTP monitor in Uptime Kuma. Returns kuma monitor id."""
    return add_monitor({
        "type": "http",
        "name": name,
        "url": url,
        "interval": interval,
        "retryInterval": 60,
        "maxretries": 3,
        "accepted_statuscodes": ["200-299"],
    })


def add_cert_monitor(name, url, interval=86400):
    """HTTP-монитор контроля срока SSL-сертификата.

    Флаг expiryNotification включает в Kuma (1.23.x) предупреждения о скором
    истечении сертификата на глобальных порогах tlsExpiryNotifyDays (7/14/21
    дней) — дедуп по notification_sent_history, максимум одно сообщение на
    порог. Проверка раз в сутки, уведомление только через вебхук VeZhPom.
    """
    return add_monitor({
        "type": "http",
        "name": name,
        "url": url,
        "interval": interval,
        "retryInterval": interval,
        "maxretries": 0,
        "accepted_statuscodes": ["200-299"],
        "expiryNotification": True,
    })


def add_push_monitor(name, interval=86400):
    """Push-монитор контроля резервных копий.

    Внешний скрипт бэкапа дёргает push-URL после успешного копирования;
    молчание дольше interval = монитор «упал» («копия не создалась»).
    Возвращает (kuma_id, push_token); токен читается из monitorList.
    """
    monitor_id = add_monitor({
        "type": "push",
        "name": name,
        "interval": interval,
        "retryInterval": interval,
        "maxretries": 0,
    })
    token = None
    for m in get_monitors():
        if m.get("id") == monitor_id:
            token = m.get("push_token")
            break
    return monitor_id, token


def delete_monitor(kuma_id):
    """Delete monitor by its Kuma id."""
    sio = _connect()
    try:
        _login(sio)
        resp = _emit(sio, "deleteMonitor", int(kuma_id))
        if not resp.get("ok"):
            raise KumaError("delete monitor failed: %s" % json.dumps(resp, ensure_ascii=False)[:200])
        return True
    finally:
        try:
            sio.disconnect()
        except Exception:
            pass


def get_monitors():
    """Return list of monitors {id, name, url, active} from Kuma.

    Kuma emits the list as a push event "monitorList" (dict id -> monitor JSON);
    the callback only confirms ok. Returns [] on any failure.
    """
    sio = _connect()
    try:
        _login(sio)
        result = {}

        def on_list(data):
            result["list"] = data

        sio.on("monitorList", on_list)
        resp = _emit(sio, "getMonitorList", None)
        if not resp.get("ok"):
            return []
        sio.sleep(2)
        raw = result.get("list") or {}
        monitors = []
        for mid in raw:
            m = raw[mid] or {}
            monitors.append({
                "id": int(mid),
                "name": m.get("name", ""),
                "url": m.get("url", ""),
                "active": m.get("active"),
                "push_token": m.get("push_token"),
            })
        return monitors
    except Exception:
        return []
    finally:
        try:
            sio.disconnect()
        except Exception:
            pass


def get_monitor_beats(monitor_id, hours=24):
    """Return heartbeat history for a monitor (oldest -> newest)."""
    sio = _connect()
    try:
        _login(sio)
        result = {}

        def cb(resp):
            result["resp"] = resp

        # Kuma ожидает два отдельных аргумента: (monitorID, period, callback).
        # python-socketio разворачивает tuple в отдельные аргументы события;
        # list уходит как ОДИН аргумент-массив, поэтому используем tuple.
        sio.emit("getMonitorBeats", (int(monitor_id), int(hours)), callback=cb)
        sio.sleep(3)
        resp = result.get("resp")
        if resp is None:
            sio.disconnect()
            raise KumaError("no response for getMonitorBeats")
        if not resp.get("ok"):
            return []
        return resp.get("data") or []
    except Exception:
        return []
    finally:
        try:
            sio.disconnect()
        except Exception:
            pass


def get_monitor_status(monitor_id):
    """Return (status, ping, msg) for the latest heartbeat of a monitor.

    status: "up" / "down" / "unknown"
    """
    beats = get_monitor_beats(monitor_id, hours=24)
    if not beats:
        return "unknown", None, None
    last = beats[-1]
    status = "up" if last.get("status") == 1 else "down"
    return status, last.get("ping"), last.get("msg")
