# ВежПом, блок 1: контроль сроков (SSL / бэкапы / домены) + светофор и дайджест

> **Статус:** архитектурный набросок для реализации. Дата: 03.11.2026.
> Стратегия и тарифы — `docs/ВежПом_дорожная_карта.md`.
> Привязки к коду соответствуют `/opt/vezpom/` на 03.11.2026 (вебхук v3.1,
> `kuma.py`, `db.py`, `vitrina_client.py`; Uptime Kuma 1.23.17, docker :3001).

---

## 1. Цель и границы

**Входим:** контроль сроков SSL-сертификатов и резервных копий (нативные механизмы
Kuma), контроль доменов (собственный модуль — в Kuma 1.23.17 его нет), ежедневный
«светофор», еженедельный отчёт, кнопки действий, анти-шум (правила из дорожной
карты §4).

**Не входим (следующие этапы):** проверка ссылок, сканер уязвимостей, эскалация,
роли, вебхуки-приёмник, белый лейбл.

**Главный принцип:** *восстановить сроковой мониторинг корректно* — то есть не
«включить уведомления Kuma как есть» (они сыплют сообщения каждый день), а
пропустить их через анти-шум ВежПома: мгновенно — только пороги 14/7/3/1 день и
пропавший бэкап; всё остальное — в светофор и отчёт.

## 2. Как работает сейчас (точки интеграции)

```
MAX-пользователь                Uptime Kuma 1.23.17 (:3001, docker)
   │  /monitor «URL»                    ▲ проверки каждые 60 с
   │                                    │ уведомление «VeZhPom - MAX» (id=2)
   ▼                                    │ HTTP POST
vezpom_webhook.py ◄─────────────────────┘ (Handler.do_POST → handle_monitor)
   │  poll_commands(): MAX long-polling /updates (отдельный поток)
   │  kuma.py: Socket.IO — add/delete мониторов, getMonitorBeats
   │  db.py: SQLite users / payments / monitors (владелец = chat<id> в имени монитора)
   ▼  vitrina_client.py → rusalts.ru: подписка (источник правды), касса YooKassa
MAX Bot API (platform-api2.max.ru): send / send_menu / answer_callback
```

Ключевые существующие функции, на которые опираемся:

| Где | Что | Зачем нам |
|---|---|---|
| `vezpom_webhook.py` `handle_monitor(data)` | Разбор вебхука Kuma, гейтинг по подписке | Точка расширения: классификация событий |
| `vezpom_webhook.py` `owner_chat_id(name)` | chat_id из имени `VeZhPom chat<id>: …` | Все новые мониторы сохраняют этот формат |
| `vezpom_webhook.py` `send/send_menu/answer_callback` | Отправка в МАКС, кнопки | Све��офор и отчёт — обычные сообщения с кнопками |
| `kuma.py` `add_http_monitor(name, url)` | Создание монитора + привязка уведомления (sparse `notificationIDList`) | Обобщить в `add_monitor(payload)` |
| `kuma.py` `get_monitor_beats(monitor_id, hours)` | История пульсаций | Проверка бэкапов/статусов для отчёта |
| `db.py` `list_monitors / add_monitor / update_monitor_status` | Мониторы владельца | Список целей для сроков и светофора |
| `vitrina_client.check_subscription` | Активна ли подписка | Гейтинг новых функций по тарифу |

## 3. Возможности Kuma 1.23.17 (проверено на контейнере)

| Нужный контроль | В Kuma 1.23.17 | Решение |
|---|---|---|
| Аптайм http/ping | ✅ уже используется | без изменений |
| **SSL-сроки** | ✅ уведомление «Certificate expiry» на канале + `certExpiryDaysRemaining` в данных | мониторы «Certificate expiry» + анти-шум ВежПома |
| **Бэкапы** | ✅ тип `push`: внешний скрипт дёргает push-URL, молчание = падение | push-монитор на период бэкапа + инструкция пользователю |
| **Домены** | ❌ нет (RDAP/WHOIS появились только в Kuma 2.x) | свой модуль `domain_watch.py` в ВежПоме (см. §6) |

> Примечание. Точный формат полей вебхука сроковых событий (`monitor.type`,
> где именно лежит число дней до истечения) зафиксировать в первый день
> реализации: один день пишем «сырой» payload в лог и подгоняем парсер.

## 4. Целевая схема блока 1

```
                      ┌────────────────────────── Uptime Kuma 1.23.17 ──────────────────────┐
                      │  http/ping (есть)   certificate-expiry (НОВОЕ)   push-бэкап (НОВОЕ)  │
                      └───────────────┬────────────────────────────────────────────────────┘
                                      │ вебхук POST :4321 (путь с секретом)
                                      ▼
   domain_watch.py ──► vezpom_webhook.py / digest.py: КЛАССИФИКАТОР + АНТИ-ШУМ
   (RDAP/WHOIS, раз        │  incident(down/up) → мгновенно (как сейчас)
    в сутки, 03:30)        │  deadline(ssl/backup) → мгновенно только пороги 14/7/3/1
                           │  всё → журнал events + открытые предупреждения warnings
                           ▼
   ┌── SQLite (новые таблицы: events, warnings, prefs) ──┐
   │                                                     │
   ▼ планировщик (поток, как poll_commands)              ▼ кнопки в МАКС
   09:00 светофор 🟢🟡🔴                                 «Проверить сейчас»
   Пн 10:00 еженедельный отчёт                           «Подробнее» «Закрыть»
```

## 5. Изменения по файлам

### 5.1 `kuma.py` — новые конструкторы мониторов

Обобщить существующий `add_http_monitor` (логика sparse `notificationIDList`
сохраняется) и добавить:

```python
def add_monitor(payload: dict) -> int          # общий add ("add" → monitorID)
def add_cert_monitor(name, url)                # type "certificate-expiry"-монитор по URL сайта
def add_push_monitor(name, period_seconds)     # type "push" → возвращает push-токен/URL
def get_monitor(monitor_id) -> dict            # тип/период/ссылка push (для push-URL)
```

*Имена мониторов — прежний формат:* `VeZhPom chat<id>: <target> · SSL`,
`VeZhPom chat<id>: <target> · BACKUP` — тогда `owner_chat_id()` продолжает
работать без изменений, а суффикс различает назначение.

### 5.2 `vezpom_webhook.py` — классификация и анти-шум

`handle_monitor(data)` дополняется ветвлением по типу события:

```python
def classify_event(data) -> dict:
    """{'kind': 'incident'|'ssl'|'backup'|'other', 'status': ..., 'days': N|None}"""

def should_notify(kind, days, warning, now) -> bool:
    """Анти-шум: incident → True; ssl/backup → True только на пороге 14/7/3/1
    (days < warning.last_level) и вне тихих часов 23:00–08:00 МСК;
    всё остальное → False (уйдёт в светофор)."""
```

Поток обработки: `classify_event` → `record_event()` (журнал) → `should_notify()` →
`send()`; `upsert_warning()` — открытое предупреждение (до выполнения условия).

Команды: `/settings` (вкл/выкл светофора и отчёта, время светофора),
`/digest` — прислать светофор сейчас. В `handle_callback` добавить payload-ы:
`incident:close:<event_id>`, `digest:send`, `digest:off`, `digest:on`,
`report:details`.

Безопасность вебхука: путь `POST /kuma/<секрет>` (секрет в `.env`), старый
корневой путь оставить на переходный период.

### 5.3 `db.py` — три новые таблицы

```sql
CREATE TABLE IF NOT EXISTS events (       -- журнал инцидентов и сроков
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER NOT NULL,
  kuma_id INTEGER,
  target TEXT,
  kind TEXT NOT NULL,            -- down | up | ssl | backup | domain
  level INTEGER DEFAULT 0,       -- порог: дни до истечения (14/7/3/1), для down/up = 0
  detail TEXT,
  started_at INTEGER,            -- время падения / фиксирования предупреждения
  resolved_at INTEGER,           -- время восстановления (для down)
  closed_at INTEGER              -- когда пользователь нажал «Закрыть инцидент»
);
CREATE TABLE IF NOT EXISTS warnings (     -- ОТКРЫТЫЕ предупреждения (дедуп анти-шума)
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER NOT NULL,
  kind TEXT NOT NULL,            -- ssl | domain | backup
  target TEXT NOT NULL,
  due_ts INTEGER,                -- расчётное время истечения / ожидаемого бэкапа
  last_level INTEGER DEFAULT 99, -- последний отправленный порог (дни); 99 = ещё не шликали
  open INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS prefs (        -- настройки оповещений на chat_id
  chat_id INTEGER PRIMARY KEY,
  traffic_light INTEGER DEFAULT 1,
  light_time TEXT DEFAULT '09:00',       -- МСК
  weekly_report INTEGER DEFAULT 1,
  weekly_time TEXT DEFAULT 'Пн 10:00',   -- МСК
  last_light_sent INTEGER DEFAULT 0,     -- идемпотентность: не слать дважды за день
  last_weekly_sent INTEGER DEFAULT 0
);
```

### 5.4 НОВОЕ `digest.py` — светофор, отчёт, планировщик

```python
def build_traffic_light(chat_id) -> str   # 🟢/🟡/🔴 из list_monitors + warnings
def build_weekly_report(chat_id, days=7) -> str
def run_scheduler(stop_event)             # поток-демон: раз в 30 с проверяет время
```

- Светофор: источники — `db.list_monitors(chat_id)` (`last_status`) + открытые
  `warnings`. Норма → одна строка «🟢 Всё в норме: N сервисов, M серверов».
  Максимум 3 предупреждения в сообщении, остальное — «…и ещё 2, /status».
- Отчёт: `events` за 7 дней — число сбоев, топ ресурса по суммарному простою
  (`resolved_at − started_at`), незакрытые предупреждения, блок «исправить
  в первую очередь» (шаблонные правила: падал ≥3 раз → хостинг; ssl ≤ 14 дней →
  продлить сертификат; backup → проверить скрипт бэкапа). Уточнение до минут —
  `kuma.get_monitor_beats(monitor_id, hours=168)` (уже реализовано).
- Планировщик: запуск отдельным потоком в `main` рядом с `poll_commands`;
  время — фиксированный МСК (UTC+3), чтобы не зависеть от TZ сервера.
  После рестарта — досылаем пропущенный светофор (проверка `last_light_sent`).
- Планировщик доменов: раз в сутки 03:30 МСК вызывает `domain_watch.check_all()`.

### 5.5 НОВОЕ `domain_watch.py` — сроки доменов (Kuma 1.23.17 этого не умеет)

```python
def expiry_date(host) -> date | None      # RDAP (rdap.org; для .ru — rdap.tcinet.ru,
                                          # проверить при реализации) → фолбэк WHOIS
def check_all()                           # по хостам мониторов активных пользователей:
                                          # upsert warnings, пороги 30/14/7 дней
```

Один запрос на домен в сутки — вежливо к регистратурам. Первая реализация —
RDAP по JSON-полю `events` (дата истечения регистрации); WHOIS-парсер — фолбэк.

## 6. Бэкапы: push-монитор + инструкция пользователю

1. `/backup` в боте → `kuma.add_push_monitor(name, period)` → бот присылает
   push-URL и одну строку-инструкцию: «добавьте последней строкой в скрипт
   бэкапа: `curl -fsS "<push-url>"`».
2. Скрипт дёргает URL после успешного копирования. Молчание дольше периода →
   Kuma шлёт вебхук → «🟡 Резервная копия не создалась» (один раз) + 🟡 в
   светофоре, пока не появится свежий пуш.
3. **Ограничение:** Kuma слушает `127.0.0.1:3001`, поэтому для бэкапов на чужих
   серверах нужен доступный снаружи URL → опубликовать push через nginx
   на `rusalts.ru` (например `Location /kuma-push/ → 127.0.0.1:3001/api/push/`).
   Это правка инфраструктуры — по AGENTS.md §8.3 согласовать с Шефом отдельно.
   Для бэкапов на самом сервере АЛТ push работает сразу по `127.0.0.1`.

## 7. Форматы сообщений

```
Светофор (09:00):
🟢 Всё в норме: 2 сайта, 1 сервер. Свет в офисе горит, я на посту.
🟡 1 предупреждение:
 • SSL rusalts.ru истекает через 9 дней
🔴 1 проблема:
 • Резервная копия site.ru не создана за последние 24 ч
 [✅ Проверить сейчас] [🔕 Отключить светофор]

Предупреждение о пороге (мгновенное, одно на порог):
🟡 SSL-сертификат rusalts.ru истекает через 7 дней.
Продлите заранее — после истечения браузеры покажут «сайт небезопасен».
 [Подробнее] [Закрыть]

Еженедельный отчёт (Пн 10:00):
📊 Неделя 27.10–02.11: сбоев 3, суммарный простой 1 ч 12 мин.
Больше всех падал site.ru (2 раза, 58 мин) — стоит разобраться с хостингом.
Незакрытые предупреждения: SSL rusalts.ru (7 дней).
Рекомендация недели: продлить сертификат rusalts.ru.
 [Подробнее]
```

## 8. Порядок реализации (PR-ы)

| PR | Содержание | Тесты |
|---|---|---|
| PR-1 | `kuma.py`: add_cert_monitor / add_push_monitor; вебхук: секретный путь + лог сырого payload 1 день (§3 примечание) | smoke на тестовом мониторе |
| PR-2 | Анти-шум + журнал: `classify_event`, `should_notify`, таблицы `events`/`warnings` | pytest: classify/should_notify (чистые функции) |
| PR-3 | Светофор: `digest.py`, `prefs`, `/settings`, `/digest` | pytest: build_traffic_light |
| PR-4 | Еженедельный отчёт + кнопки «Проверить/Подробнее/Закрыть» | pytest: build_weekly_report |
| PR-5 | Домены: `domain_watch.py` (RDAP/WHOIS) | pytest: парсер даты |
| chore | Перенос `/opt/vezpom` → `bot/vezpom/` в монорепо (AGENTS.md §1), systemd-unit, деплой из CI | CI зелёный |

## 9. Риски и ограничения

1. **Формат сроковых вебхуков** не проверен вживую → PR-1 логирует сырой payload,
   парсер подгоняется по факту (риск малый, локализован одним модулем).
2. **RDAP для .ru** — проверить доступность `rdap.tcinet.ru`; фолбэк — WHOIS
   (`paid-till`). Зоны без RDAP/WHOIS-даты: предупреждение «не удалось определить
   срок» вместо молчания.
3. **Push для чужих серверов** требует внешнего URL (nginx на rusalts.ru) —
   правка инфраструктуры, согласование по §8.3 AGENTS.md.
4. **Kuma 2.x** при апгрейде меняет протокол Socket.IO — `kuma.py` потребует
   переработки; не блокирует блок 1, апгрейд — отдельное решение после MVP.
5. **Шум Kuma при истечении сертификата** (сообщает ежедневно после порога) —
   гасится дедупом `warnings.last_level`: повторный порог не отправляется.
