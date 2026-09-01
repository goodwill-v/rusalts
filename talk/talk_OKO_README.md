# `/talk` и OpenClaw (ОКО): функционал, подключение, архитектура, перенос на другие проекты

Документ описывает страницу **`/talk`** в приложении ALT, её API, связку с **OpenClaw**, шаги для пользователя и администратора, текущую и рекомендуемую архитектуру при переносе на другие продукты, возможность подключения **других бэкендов** к той же странице и отдельное **техническое задание** на аналогичный UI для OpenClaw.

Общая установка OpenClaw, RouterAI и Gateway: см. [`OKO_README.md`](./OKO_README.md).

---

## 1. Функционал `/talk` (что умеет страница и бэкенд)

### 1.1. Страница в браузере

- **URL:** `https://<ваш-домен>/talk` (в коде страница называется скрытой интеграцией: отдельный HTML без пункта в меню).
- **Статика:** `GET /talk/assets/talk.css`, `GET /talk/assets/talk.js` — из каталога репозитория `talk/public/`.
- **Шлюз по ключу:** пользователь вводит **`TALK_KEY`** в форме на странице; ключ сохраняется в **`localStorage`** браузера и дальше передаётся в каждый запрос к API как `Authorization: Bearer …` или `X-Talk-Key: …`.
- **Чат:** отправка текста; опционально — вложение файла (multipart). Ответ показывается в логе переписки.
- **Входящие от приложений:** фронт периодически опрашивает **`GET /api/talk/inbox`** и подмешивает события в ленту (сообщения, пришедшие через **`POST /api/talk/incoming`** от внешних систем).
- **Панель управления (одна строка):** выпадающий список **модели**, кнопки **Статус**, **Стоп**, **Старт** (управление `openclaw-gateway.service`).
- **Нижняя панель (одна строка, удобно на телефоне):** **+** (файл), **Новая** (сброс сессии), **Экспорт**, **Отпр.**, иконка **микрофона** (голос → текст).
- **localStorage в браузере:** `talk_key`, `talk_model`, `talk_oko_admin` (админ-ключ для Стоп/Старт), `talk_last_inbox_id`.

### 1.2. API префикса `/api/talk`

| Метод и путь | Кто авторизован | Назначение |
|--------------|-----------------|------------|
| `GET /api/talk/ping` | `TALK_KEY` | Проверка ключа и доступности API. |
| `GET /api/talk/models` | `TALK_KEY` | Список моделей из `openclaw.json` (`agents.defaults.models`) + `default`. |
| `GET /api/talk/upstream-health` | `TALK_KEY` | Проверка цепочки до relay: запрос на `…/health` у upstream (без LLM). |
| `POST /api/talk/relay` | `TALK_KEY` | JSON `{"text":"…","model":"openai/…"}` (model опционально) → relay. |
| `POST /api/talk/relay-file` | `TALK_KEY` | `multipart`: `text`, `file`, опционально `model` → relay. |
| `POST /api/talk/session/reset` | `TALK_KEY` | Сброс сессии OpenClaw `talk-relay` (кнопка «Новая»). |
| `GET /api/talk/oko/status` | `TALK_KEY` | Статус `systemctl is-active openclaw-gateway`. |
| `POST /api/talk/oko/stop` | `TALK_KEY` + **`X-Oko-Admin`** | Остановить Gateway. |
| `POST /api/talk/oko/start` | `TALK_KEY` + **`X-Oko-Admin`** | Запустить Gateway. |
| `POST /api/talk/incoming` | **`X-Talk-App-Token`** | Внешнее приложение кладёт сообщение/файл в inbox (`data/talk/`). |
| `GET /api/talk/inbox?after=<id>` | `TALK_KEY` | Новые события inbox (до 200). |
| `GET /api/talk/file/{name}` | `TALK_KEY` | Файл из inbox. |

Таймауты HTTP‑клиента бэкенда к relay завышены (порядка **130–180 с**), чтобы длинные ответы LLM не обрывались преждевременно.

### 1.3. Relay OpenClaw (отдельный процесс)

- Сервис **`openclaw-talk-relay`**: FastAPI на **`172.17.0.1:19010`** (только хост, не в интернет).
- Проверка **`X-App-Key`** против **`TALK_RELAY_APP_KEY`**.

| Relay | Назначение |
|-------|------------|
| `GET /health` | Живость relay. |
| `GET /models` | Каталог моделей из `OPENCLAW_CONFIG_PATH`. |
| `POST /session/reset` | Удаление сессии `talk-relay` из `sessions.json` и transcript. |
| `POST /talk` | Запрос к агенту (JSON или multipart). |
| `GET/POST /oko/gateway/*` | Статус / стоп / старт Gateway (стоп/старт + **`X-Oko-Admin`**). |

- **`POST /talk`**: при наличии поля **`model`** в теле вызывается  
  `openclaw agent --session-id <id> --model <id> --message … --json --timeout 180`.
- Для файла — подсказка с путём и до **8000** символов UTF‑8 в prompt.
- Внутренний таймаут процесса **~90 с** (ниже таймаута прокси в `talk.py`).

### 1.4. Ошибки в интерфейсе

При сбоях LLM/RouterAI в чате показывается понятное сообщение:

**«Модель недоступна на стороне провайдера»** + краткая техническая часть  
(например `incomplete_result`, `FailoverError`, `no-speech` не относится к LLM).

Рекомендуется увеличить **`proxy_read_timeout`** в Nginx для `/talk` и API (порядка **180 с**), иначе браузер получит **504** раньше, чем ответ от backend (~90–130 с).

Итог для пользователя чата: **каждое сообщение** уходит в **одну и ту же** OpenClaw‑сессию (`OPENCLAW_TALK_SESSION_ID`, по умолчанию `talk-relay`), контекст на стороне OpenClaw накапливается в рамках этой сессии.

---

## 2. Текущая архитектура (как сейчас устроено)

```text
[Браузер /talk]
    │  TALK_KEY (Bearer / X-Talk-Key)
    ▼
[FastAPI в Docker: сервис web]
    │  httpx POST + X-App-Key: TALK_RELAY_APP_KEY
    │  TALK_RELAY_URL → например http://host.docker.internal:19010/talk
    ▼
[Хост: openclaw-talk-relay :19010 на 172.17.0.1]
    │  subprocess: openclaw agent …
    ▼
[Хост: OpenClaw Gateway + state в ~/.openclaw]
    │
    ▼
[RouterAI https://routerai.ru/api/v1]
```

Дополнительно:

- **`extra_hosts: host.docker.internal:host-gateway`** в `docker-compose.yml` у сервиса `web`, чтобы из контейнера достучаться до relay на хосте.
- **Gateway** слушает только **loopback** (например `127.0.0.1:19001`); в интернет напрямую не выставляется.
- **Официальный веб OpenClaw (Control UI / Dashboard)** — отдельный вход: `openclaw dashboard`, SSH‑туннель, путь вида `/__openclaw__/` на порту Gateway; к странице `/talk` это **не** привязано (два разных UI).

---

## 3. Действия пользователя (человек с браузером)

1. Открыть **`https://<домен>/talk`**.
2. Ввести выданный администратором **`TALK_KEY`** и подтвердить (ключ попадёт в `localStorage` этого браузера).
3. Нажать проверку связи, если на странице есть такой шаг, или сразу отправить сообщение — при ошибке ключа сервер ответит **401**.
4. Писать сообщения и при необходимости прикреплять файлы; ждать ответа (ответ может занимать **до 1–2 минут** из‑за LLM).

Пользователю **не** нужны ключи RouterAI или OpenClaw Gateway — только **`TALK_KEY`**.

### 3.1. Установка на Android (PWA)

На телефоне в **Chrome**: откройте `https://<домен>/talk`, войдите по ключу → меню **⋮** → **«Установить приложение»** или **«Добавить на главный экран»**.

Файлы PWA в репозитории:

| Файл | URL |
|------|-----|
| `talk/public/manifest.webmanifest` | `/talk/assets/manifest.webmanifest` |
| `talk/public/icons/icon-192.png` | `/talk/assets/icons/icon-192.png` |
| `talk/public/icons/icon-512.png` | `/talk/assets/icons/icon-512.png` |

Имя на экране: **ТОЛК**, режим `standalone` (без адресной строки Chrome).

### 3.2. Интерфейс (мобильный и десктоп)

**Верхняя строка:** модель · Статус · Стоп · Старт (без дублирующей надписи «Gateway: active» — статус в подсказке кнопки **Статус**).

**Нижняя строка:** `+` · Новая · Экспорт · Отпр. · 🎤  
На широком экране: «Новая сессия», «Отправить».

| Кнопка | Действие |
|--------|----------|
| **Новая** | `POST /api/talk/session/reset` + очистка ленты на экране. |
| **Экспорт** | Скачивание `talk-export-*.md` из истории чата в браузере. |
| **Отпр.** | Отправка текста/файла с выбранной моделью. |
| **🎤** | Голос → текст (Web Speech API в Chrome). Слушает до нажатия **Остановить** (иконка подсвечена), затем автоотправка. |

### 3.3. Голос в ТОЛК ≠ голосовой диалог в Dashboard

| | **ТОЛК (🎤)** | **Dashboard (Start Talk)** |
|---|----------------|---------------------------|
| Механизм | Web Speech API → **текст** → `openclaw agent` | WebRTC / realtime → **голос ответа** |
| Браузер | Chrome / Edge на Android; **не Firefox** | Chrome + SSH-туннель на Gateway |
| Сеть | Часто нужен доступ к **Google Speech** (VPN в РФ) | `talk.realtime` + OpenAI/Google API |
| Настройка | Не требует `talk` в `openclaw.json` | Нужен блок **`talk.realtime`** в конфиге |

Подробнее про Dashboard: [`OKO_README.md`](./OKO_README.md) §5.

### 3.4. Если интерфейс не дождался ответа (таймаут, 504)

Иногда в чате появляется **504 Gateway Time-out** (HTML от nginx) или сообщение «зависло» без ответа. Это значит: **браузер или прокси оборвал ожидание**, а запрос на сервере **иногда уже обработан** (ответ ушёл в OpenClaw, но не успел отобразиться в ТОЛК).

**Не паникуйте и не отправляйте одно и то же длинное сообщение много раз подряд** — каждое нажатие **Отпр.** = новый запрос к LLM (дольше, дороже, путаница в контексте сессии `talk-relay`).

#### Что делать по шагам

1. **Подождите 10–20 секунд** — ответ мог уже уйти в OpenClaw, а в ленту просто не попал из‑за обрыва.
2. **Обновите страницу** `/talk` (на телефоне — закрыть вкладку/приложение и открыть снова; на ПК — **Ctrl+Shift+R**). При необходимости снова введите **TALK_KEY**.
3. **Отправьте одно короткое уточнение**, а не копию длинного текста, например:  
   *«Повтори последний ответ кратко»* или *«Ты получил моё предыдущее сообщение?»*  
   Сессия помнит контекст — модель часто «догонит» без повторения всего задания.
4. Если снова таймаут — **упростите запрос** (меньше текста, без файла) или нажмите **Новая** и кратко опишите задачу заново (контекст OpenClaw сбросится).

#### Когда нажимать «Новая»

- После нескольких обрывов диалог «поехал».
- Нужен **чистый** разговор без хвостов от неудачных попыток.
- После **Новая** имеет смысл **кратко** сформулировать задачу заново.

#### Что уже настроено на сервере

На **rusalts.ru** таймаут nginx перед приложением увеличен до **180 с** (~3 мин). Обычный ответ должен доходить до чата. Если снова видите **504** — жёсткое обновление страницы и **одна** повторная отправка (короче предыдущей).

#### Когда обращаться к администратору

| Симптом | Вероятная причина |
|---------|-------------------|
| **504** постоянно, даже на коротких сообщениях | Таймаут прокси или перегрузка сервера |
| Текст **«Модель недоступна на стороне провайдера»** (часто после 504) | Сессия `talk-relay` в «битом» состоянии — нажмите **Новая** и повторите; при `incomplete_result` помогает полный сброс сессии |
| Текст **«Модель недоступна…»** без предшествующего 504 | RouterAI / модель / лимиты |
| **Статус** / **Стоп** / **Старт** — Gateway не active | ОКО остановлен или не запущен |

**Памятка:** таймаут в браузере ≠ «сообщение потерялось». Сначала подождать → обновить → одно короткое уточнение → при необходимости **Новая**. Не дублировать длинные запросы.

---

## 4. Действия администратора / разработчика (подключить `/talk` к OpenClaw)

Выполняется **один раз** на сервере (или при смене окружения).

### 4.1. OpenClaw и RouterAI

1. Установить и настроить OpenClaw по [`OKO_README.md`](./OKO_README.md): `openclaw.json`, `gateway-token.env`, `routerai.env`, systemd **`openclaw-gateway.service`**.
2. **Модель по умолчанию для ТОЛК** (в `openclaw.json`):

```json
"agents": {
  "defaults": {
    "model": { "primary": "deepseek/deepseek-v4-flash" },
    "models": {
      "deepseek/deepseek-v4-flash": {},
      "deepseek/deepseek-v4-pro": {},
      "openai/gpt-5.4-mini": {},
      ...
    }
  }
}
```

В каталоге `models.providers.deepseek.models` должны быть id **`deepseek/deepseek-v4-flash`**, **`deepseek/deepseek-v4-pro`**.  
CLI: `openclaw models set deepseek/deepseek-v4-flash`.

3. Убедиться, что с хоста выполняется:

```bash
/usr/bin/node /usr/lib/node_modules/openclaw/openclaw.mjs agent \
  --session-id talk-relay --model deepseek/deepseek-v4-flash \
  --message "ping" --json
```

4. После смены модели или сброса сессии в UI — при необходимости **`POST /api/talk/session/reset`** или кнопка **Новая** на `/talk`.

### 4.2. Relay

1. Разместить **`talk/openclaw_relay.py`** в проекте (например `/opt/alt/talk`).
2. Создать venv с зависимостями (**`fastapi`**, **`uvicorn`**, **`python-multipart`** и т.д. по факту импортов).
3. Установить unit **`openclaw-talk-relay.service`**: `EnvironmentFile` из `/opt/alt/.env` + `/root/.openclaw/gateway-token.env` + `/root/.openclaw/routerai.env`, переменные `OPENCLAW_CONFIG_PATH`, `OPENCLAW_STATE_DIR`, `OPENCLAW_BIN`, при необходимости `OPENCLAW_TALK_SESSION_ID`.
4. `sudo systemctl enable --now openclaw-talk-relay`.

### 4.3. Приложение ALT (Docker)

1. В **`/opt/alt/.env`** задать:
   - **`TALK_KEY`** — длинный случайный секрет для людей/клиентов страницы `/talk`.
   - **`TALK_RELAY_URL`** — URL эндпоинта relay, **доступный из контейнера** `web`, например `http://host.docker.internal:19010/talk`.
   - **`TALK_RELAY_APP_KEY`** — секрет, совпадающий с relay (`EXPECTED_APP_KEY`).
   - **`TALK_APP_TOKEN`** — секрет для **`POST /api/talk/incoming`**.
   - **`TALK_OKO_ADMIN_KEY`** — секрет для кнопок **Стоп/Старт** Gateway (заголовок `X-Oko-Admin`; тот же ключ на relay).
2. В **`docker-compose.yml`** у сервиса `web` оставить **`extra_hosts`** для `host.docker.internal`.
3. В коде приложения: подключён роутер **`app.routers.talk`**, страница **`/talk`**, монтирование **`/talk/assets`**, конфиг читает переменные из **`app/config.py`**.
4. Пересборка и перезапуск:  
   `docker compose up -d --build web`  
   `sudo systemctl restart openclaw-talk-relay`

### 4.4. Проверка

- С заголовком **`TALK_KEY`:** `GET /api/talk/ping`, `GET /api/talk/upstream-health`.
- С браузера: отправка текста на `/talk`.

---

## 5. Оптимальная архитектура для **других проектов** (на базе этой разработки)

Цель: переиспользовать проверенную схему «веб‑чат → один HTTP upstream → исполнитель», не копируя весь ALT.

### 5.1. Рекомендуемая последовательность

1. **Инфраструктура LLM** на хосте или в отдельном сервисе: OpenClaw Gateway + секреты + провайдер (RouterAI и т.д.) — как в `OKO_README.md`.
2. **Исполнитель запросов** — один из вариантов:
   - **A (минимальный перенос):** тот же **`openclaw_relay.py`** + systemd unit; контракт: `POST /talk`, `GET /health`, заголовок **`X-App-Key`**.
   - **B (чище для продукта):** вынести relay в отдельный микросервис с образом Docker, сеть `host` или явный IP; тогда в `.env` нового проекта только URL и ключи.
3. **Веб‑приложение:** либо портировать блок **`/talk`** (см. файлы ниже), либо реализовать ТЗ из §8, сохранив тот же контракт API к relay.
4. **Сеть:** если бэкенд в Docker, а relay на хосте — сохранить паттерн **`host.docker.internal`** или эквивалент (Linux `host-gateway`).
5. **Наблюдаемость:** логи relay (`journalctl`) и логи web; алерты по **`/api/talk/upstream-health`**.

### 5.2. Какие **файлы взять** из этого проекта (чеклист)

| Файл / каталог | Назначение |
|----------------|------------|
| `talk/openclaw_relay.py` | Relay под OpenClaw CLI. |
| `talk/public/index.html` | Оболочка страницы `/talk`. |
| `talk/public/talk.js` | Логика ключа, отправки, inbox. |
| `talk/public/talk.css` | Стили. |
| `talk/public/manifest.webmanifest` | PWA: установка на главный экран / Android. |
| `talk/public/icons/icon-192.png`, `icon-512.png` | Иконки PWA. |
| `app/routers/talk.py` | Прокси relay + incoming/inbox/file. |
| `app/config.py` (фрагмент `TALK_*`) | Чтение переменных окружения. |
| `app/main.py` (фрагмент: `include_router(talk)`, mount `/talk/assets`) | Подключение в FastAPI. |
| `app/routers/pages.py` (маршрут `GET /talk`) | Отдача HTML. |
| `docker-compose.yml` (фрагмент `extra_hosts` для `web`) | Доступ к хосту из контейнера. |
| `/etc/systemd/system/openclaw-talk-relay.service` (как образец) | Автозапуск relay на хосте. |

Не копировать в Git другого проекта: **`.env`**, ключи, серверные пути — только описать в README нового репозитория.

### 5.3. Улучшения «второй итерации»

- Вынести **`TALK_RELAY_URL`** на внутреннее DNS‑имя (`relay.internal`) вместо Docker magic host.
- Разделить **`TALK_KEY`** (люди) и **`TALK_APP_TOKEN`** (машины) обязательно разными значениями.
- Для нескольких ботов — несколько relay‑процессов или один relay с маршрутизацией по заголовку/префиксу URL (потребует доработки кода).

---

## 6. Можно ли к странице `/talk` подключить **другие приложения**, не только ОКО?

**Да**, в двух разных смыслах.

### 6.1. Другой «мозг» вместо OpenClaw, но та же страница `/talk`

Страница и **`/api/talk/relay*`** завязаны только на **`TALK_RELAY_URL`**: любой HTTP‑сервис с тем же контрактом может стоять за relay.

Что нужно сделать:

1. Реализовать сервис с **`POST /talk`** (принимает JSON или multipart как relay) и **`GET /health`**, проверкой **`X-App-Key`**.
2. В **`.env`** сменить **`TALK_RELAY_URL`** на URL этого сервиса (и при необходимости **`TALK_RELAY_APP_KEY`**).
3. Перезапустить **`web`**; relay OpenClaw можно остановить, если не используется.

OpenClaw при этом **не** обязателен для работы `/talk`.

### 6.2. Другие приложения как **источники сообщений** в ту же ленту `/talk`

Уже предусмотрено API **`POST /api/talk/incoming`**:

- Авторизация заголовком **`X-Talk-App-Token`** (значение из **`TALK_APP_TOKEN`** или, если пусто, **`TALK_KEY`**).
- Тело: `multipart` с полями `text` и/или `file`.
- События попадают в **`data/talk/inbox.jsonl`**; страница забирает их через **`GET /api/talk/inbox`**.

Типичные сценарии: бот VK, внутренняя CRM, скрипт мониторинга — всё, что может делать HTTP POST с токеном.

### 6.3. Ограничения

- **`/talk`** не встраивает iframe **OpenClaw Control UI**; это отдельный продуктовый объём (SSO, безопасность, CORS).
- Одновременно «в ответ» пользователю идёт **одна** цепочка **`TALK_RELAY_URL`**; мульти‑бот на одной странице без доработки фронта не поддерживается.

---

## 7. Техническое задание: веб‑интерфейс чата для OpenClaw (аналог `/talk`, самостоятельный продукт)

Ниже ТЗ можно выдать как отдельный документ для разработки **без** привязки к ALT.

### 7.1. Цель

Веб‑страница «Чат с агентом OpenClaw»: пользователь вводит сообщение, получает ответ; опционально — файл; опционально — входящие события из внешних систем. Безопасный доступ по секрету или SSO (на выбор заказчика).

### 7.2. Роли и сценарии

- **Пользователь чата:** отправка текста/файла, просмотр истории в рамках сессии браузера.
- **Администратор:** выдача/ротация ключей, просмотр логов, настройка URL исполнителя.
- **Внешняя система:** push сообщений в ленту (аналог `incoming`).

### 7.3. Функциональные требования

1. Форма ввода текста и кнопка «Отправить»; индикатор ожидания; отображение ошибок (401, 502, текст upstream по политике безопасности).
2. Загрузка файла; передача на бэкенд; отображение ответа агента.
3. Хранение **клиентского** ключа доступа (минимум — как сейчас в `localStorage`; лучше — сессия на сервере + httpOnly cookie при появлении нормальной аутентификации).
4. Эндпоинт проверки «живости» цепочки до исполнителя (аналог `upstream-health`).
5. (Опционально) Long polling или SSE для входящих событий вместо частого опроса `inbox`.

### 7.4. Нефункциональные требования

- Таймауты HTTP: не меньше **120 с** для LLM‑маршрута.
- Ограничение размера тела запроса и файла (согласовать с прокси Nginx).
- Логирование: request id, без логирования полного `TALK_KEY` и `X-App-Key`.
- Защита от CSRF при cookie‑сессии; при Bearer из SPA — осознанная модель XSS.

### 7.5. Интеграция с OpenClaw

Минимум один из вариантов:

- **Через subprocess / CLI** (как relay сейчас): просто, но привязка к хосту и PATH.
- **Через Gateway WebSocket/API** (если есть стабильный клиентский протокол в вашей версии OpenClaw): предпочтительнее для горизонтального масштаба и отмены запросов.

Контракт между «веб‑бэкендом» и «исполнителем» зафиксировать документом: URL, метод, заголовки, формат JSON ответа (`reply` или полный JSON агента).

### 7.6. Критерии приёмки

- Успешный ответ на тестовую фразу при включённом OpenClaw.
- Корректная ошибка при неверном ключе.
- Корректная ошибка при недоступном relay.
- Загрузка файла не ломает процесс (лимиты и понятная ошибка при превышении).
- (Если реализован incoming) событие, отправленное `curl` с токеном, появляется в UI в течение согласованного интервала опроса.

### 7.7. Вне скоупа (явно)

- Полная копия OpenClaw Control UI (настройка каналов, skills, произвольный exec).
- Мульти‑тенантность и биллинг без отдельного ТЗ.

---

## 8. Ссылки внутри набора документов

| Документ | Содержание |
|----------|------------|
| [`OKO_README.md`](./OKO_README.md) | Установка OpenClaw, RouterAI, Gateway, Dashboard, systemd. |
| Этот файл | `/talk`, relay, перенос, другие приложения, ТЗ на UI. |

---

## 9. Подключение Telegram (коротко)

Ниже — **рабочая схема**, которая сейчас используется: **Telegram bot → OpenClaw Gateway → RouterAI**, при этом `/talk` продолжает работать параллельно через relay.

### 9.1. Что должно быть на сервере

- **Файл токена Telegram**: `/root/.openclaw/telegram.env`

```bash
TELEGRAM_BOT_TOKEN=123456:ABCDEF...
```

- **Gateway unit** (`/etc/systemd/system/openclaw-gateway.service`) должен подхватывать этот env (добавить в файл):
  - `EnvironmentFile=/root/.openclaw/telegram.env`

### 9.2. Быстрые шаги подключения

1. Включить Telegram‑плагин (1 раз):

```bash
openclaw plugins enable telegram
sudo systemctl restart openclaw-gateway
```

2. Включить канал и доступ только для себя (allowlist):

```bash
openclaw config set channels.telegram.enabled true
openclaw config set channels.telegram.dmPolicy allowlist
openclaw config set channels.telegram.allowFrom '["tg:<YOUR_TELEGRAM_USER_ID>"]'
sudo systemctl restart openclaw-gateway
```

3. Если используется pairing (или бот сам выдал pairing code) — одобрить (опционально):

```bash
openclaw pairing approve telegram <PAIRING_CODE>
```

4. Если канал падает с `Cannot find module 'grammy'` — поставить runtime‑зависимости плагинов:

```bash
openclaw plugins deps --repair
sudo systemctl restart openclaw-gateway
```

### 9.3. Типовые ошибки и быстрые решения

- **`Cannot find module 'grammy'`**
  - **Причина**: не материализованы bundled runtime deps для Telegram‑плагина.
  - **Решение**: `openclaw plugins deps --repair`, затем рестарт Gateway.

- **`sendChatAction failed` / `fetch timeout` / таймауты к Telegram API**
  - **Причина**: сеть/IPv6/DNS (часто AAAA резолвится, но IPv6‑маршрут не работает).
  - **Проверка**: `curl -4 -I https://api.telegram.org` и `curl -6 -I https://api.telegram.org`.
  - **Решение**: заставить ходить по IPv4 (настройка приоритета IPv4 или отключение IPv6) и перезапуск Gateway.

- **`All models are temporarily rate-limited` / `429`**
  - **Причина**: лимит/биллинг/квота у провайдера (в нашем случае RouterAI).
  - **Решение**: снять лимит/пополнить/поменять модель или настроить fallback.

- **Бот пишет “access not configured”**
  - **Причина**: `dmPolicy` не допускает этого пользователя (pairing не одобрен, allowlist пуст).
  - **Решение**: `openclaw pairing approve …` или `channels.telegram.allowFrom` + `dmPolicy=allowlist`.

---

## 10. Кнопки «Стоп / Старт» Gateway на `/talk`

Для сценария «в неурочное время не грузить сервер» на странице `/talk` можно остановить **`openclaw-gateway.service`** (Telegram и Control UI на этом хосте перестанут отвечать, пока не нажмёте **Старт**). **Relay** (`openclaw-talk-relay`) при этом остаётся запущенным, чтобы кнопка **Старт** снова подняла Gateway.

Первая настройка **Стоп/Старт** запрашивает **`TALK_OKO_ADMIN_KEY`** (сохраняется в `localStorage` как `talk_oko_admin`).

### Переменные в `/opt/alt/.env`

| Переменная | Назначение |
|------------|------------|
| **`TALK_OKO_ADMIN_KEY`** | Секрет для операций **Стоп/Старт** (заголовок `X-Oko-Admin` с браузера). Должен совпадать на **relay** (relay читает тот же `.env`). |

Обычный **`TALK_KEY`** по-прежнему нужен для входа на `/talk`; без **`TALK_OKO_ADMIN_KEY`** на сервере эндпоинты стоп/старт вернут **501**.

### API

- `GET /api/talk/oko/status` — статус `systemctl is-active openclaw-gateway` (нужен только `TALK_KEY`).
- `POST /api/talk/oko/stop` / `POST /api/talk/oko/start` — `TALK_KEY` + заголовок **`X-Oko-Admin: <TALK_OKO_ADMIN_KEY>`**.

Relay принимает те же вызовы на `…/oko/gateway/*` с **`X-App-Key`**; для стоп/старт дополнительно **`X-Oko-Admin`**.

### Требования

- Пользователь **`openclaw-talk-relay`** в systemd должен иметь право выполнять **`systemctl start/stop openclaw-gateway`** (в текущей схеме часто **`User=root`** на relay).

---

## 11. Control UI: «protocol mismatch» после обновления OpenClaw

Если в Dashboard после `systemctl restart openclaw-gateway` появляется **protocol mismatch**:

- Gateway обновился (протокол **v4**), а в браузере закэширован старый Control UI (**v3**, например `2026.4.29`).
- **Решение:** жёсткое обновление страницы (**Ctrl+Shift+R**), очистка данных сайта для `localhost:19001`, или приватное окно; убедиться, что открыт URL из `openclaw dashboard` после обновления пакета.

ТОЛК на это не влияет — отдельный UI без WebSocket Gateway.

---

## 12. Синхронизация кода (репозиторий ALT ↔ сервер)

Исходники страницы `/talk` в проекте ALT:

| Путь на сервере | Путь в репозитории (пример) |
|-----------------|----------------------------|
| `/opt/alt/talk/public/*` | `talk/public/*` |
| `/opt/alt/talk/openclaw_relay.py` | `talk/openclaw_relay.py` |
| `/opt/alt/app/routers/talk.py` | `app/routers/talk.py` |

После правок на сервере:

```bash
docker compose -f /opt/alt/docker-compose.yml build web
docker compose -f /opt/alt/docker-compose.yml up -d web
sudo systemctl restart openclaw-talk-relay
```

---

## 13. Журнал изменений (2026-05-20)

- Кнопки **Стоп/Старт** Gateway на `/talk` (`TALK_OKO_ADMIN_KEY`, API `oko/*`).
- Выпадающий список **моделей**; передача `model` в relay; дефолт **`deepseek/deepseek-v4-flash`**.
- Сообщение об ошибке **«Модель недоступна на стороне провайдера»**.
- Панель действий: **Новая сессия**, **Экспорт**, **Отпр.**, **🎤** (голос); мобильная вёрстка в одну строку.
- API **`GET /models`**, **`POST /session/reset`**.
- **PWA:** `manifest.webmanifest`, иконки **ТОЛК** для установки на Android.
- **§3.4:** памятка пользователю при таймауте / 504; nginx **180 с** на `rusalts.ru`.

---

*Актуальность: 2026-05-20 — ALT (`/opt/alt`) + OpenClaw + relay на хосте `109.73.202.123`, Docker `web`, домен `rusalts.ru/talk`.*
