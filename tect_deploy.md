# Тестовая страница на rusalts.ru — что сделать на сервере

Кратко: статическая витрина лежит в каталоге сайта, а виджет консультанта (FastAPI) — за Nginx как reverse proxy к локальному `uvicorn`.

## Что уже должно быть

- Домен **rusalts.ru** с SSL (Certbot), Nginx с `nginx -t` без ошибок.
- Каталог сайта: **`/var/www/rusalts.ru/html`** (владелец **`www-data`** для файлов внутри).
- Приложение: **`/opt/alt`** (код + виртуальное окружение **`.venv`**).
- Юнит systemd: **`alt-consultant`** — поднимает **uvicorn** на **`127.0.0.1:8000`**.
- В Nginx для этого сайта настроены **прокси** на бэкенд (минимум):  
  **`/widget`**, **`/api`**, **`/static`**, **`/health`** → `http://127.0.0.1:8000`  
  остальные запросы — статика из `html` (в т.ч. главная **`/`**).

Файлы конфигурации в репозитории (эталон): `deploy/nginx-rusalts.ru.conf`, `deploy/systemd/alt-consultant.service`, пример окружения: `deploy/env.production.example`.

## Первичная установка (если разворачиваете с нуля)

1. Установить **`python3-venv`** (и при необходимости зависимости для сборки пакетов).
2. Скопировать код проекта в **`/opt/alt`** (без локального `.venv` из разработки).
3. Создать окружение и зависимости:
   ```bash
   cd /opt/alt
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```
4. Скопировать **`deploy/env.production.example`** в **`/opt/alt/.env`** и при необходимости поправить переменные (домен, CORS, ключи VK позже).
5. Права:
   ```bash
   chown -R www-data:www-data /opt/alt /var/www/rusalts.ru/html
   ```
6. Установить systemd:
   ```bash
   install -m 644 /opt/alt/deploy/systemd/alt-consultant.service /etc/systemd/system/alt-consultant.service
   systemctl daemon-reload
   systemctl enable --now alt-consultant
   ```
7. Подключить конфиг Nginx (см. `deploy/nginx-rusalts.ru.conf`), сделать бэкап текущего `sites-available`, затем:
   ```bash
   nginx -t && systemctl reload nginx
   ```
8. Положить статику витрины в **`/var/www/rusalts.ru/html/`** из репозитория: **`deploy/public/`** (`index.html`, `landing.css`).

## Обновление тестовой страницы и кода

С машины, где есть репозиторий и SSH к серверу, из **корня проекта**:

```bash
bash deploy/sync-to-server.sh
```

Скрипт синхронизирует код в `/opt/alt`, статику в `html`, перезапускает **`alt-consultant`**. Локальный файл **`.env` не копируется** — секреты правят только в **`/opt/alt/.env`** на сервере.

## Проверка

На сервере:

```bash
systemctl status alt-consultant
curl -sS http://127.0.0.1:8000/health
```

Снаружи в браузере:

- **https://rusalts.ru/** — тестовая витрина «АЛЬТЕРНАТИВА» и iframe с виджетом;
- **https://rusalts.ru/widget** — только виджет;
- **https://rusalts.ru/health** — `{"status":"ok"}`.

## Если что-то не открывается

1. **`systemctl status alt-consultant`** и **`journalctl -u alt-consultant -n 50`** — ошибки uvicorn или `.env`.
2. **`nginx -t`** и логи Nginx — не сломался ли прокси после правок.
3. Убедиться, что порт **8000** слушает только **127.0.0.1** (как в юните), а снаружи идёт HTTPS через Nginx.

## Отключение теста позже

```bash
systemctl disable --now alt-consultant
```

Удалить или закомментировать в конфиге сайта `location` для `/widget`, `/api`, `/static` и т.д., затем `nginx -t && systemctl reload nginx`.
