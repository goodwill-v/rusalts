# Staging — черновик сайта для проверки (АЛТ / rusalts)

**Статус:** работает (01.09.2026). HTTPS-домен — готов к включению (ждёт DNS).

## Зачем staging

- Проверять новые версии кода **до** прода, не рискуя боевым сайтом.
- Агенты (L3) могут деплоить на staging свободно, на прод — только через релиз.
- **Ссылку на staging удобно давать Шефу в чате** — он открывает её прямо в дашборде.

## Как устроено

| | Прод | Staging |
|---|------|---------|
| Ветка | `main` | `staging` |
| Порт | 8000 (через nginx → 443) | 8001 |
| Проект compose | `alt` (по умолчанию) | `alt-staging` |
| .env | `.env` | `.env.staging` |
| Данные (runtime) | `./data` | `./data_staging` |
| URL | https://rusalts.ru | http://109.73.202.123:8001 (пока) → https://staging.rusalts.ru (после DNS) |

## HTTPS (staging.rusalts.ru) — как включить

Статус: **готово к включению** (nginx-конфиг создан в sites-available), ждёт DNS от Шефа.

1. В панели Timeweb: создать A-запись `staging` → `109.73.202.123` (для домена rusalts.ru).
2. На сервере (сделает Алик):
   ```bash
   ln -s /etc/nginx/sites-available/staging.rusalts.ru /etc/nginx/sites-enabled/
   certbot --nginx -d staging.rusalts.ru
   nginx -t && systemctl reload nginx
   ```
3. Перезапустить staging, чтобы приложение знало свой HTTPS-адрес:
   ```bash
   cd /opt/alt && docker compose -f docker-compose.staging.yml --env-file .env.staging up -d --build
   ```
4. Проверка: `curl -sS https://staging.rusalts.ru/health` → `{"status":"ok"}`

## Файлы

- `docker-compose.staging.yml` — конфиг staging (порт 8001, свои volume, образ `alt-expert-web:staging`).
- `.env.staging` — копия `.env` прода с правками (см. ниже). **Секреты — только на сервере.**
- `/etc/nginx/sites-available/staging.rusalts.ru` — nginx-конфиг (прокси на 8001, HTTPS).

## Настройка .env.staging (на сервере)

```bash
cd /opt/alt
cp .env .env.staging
# поправить:
#   PUBLIC_BASE_URL=https://staging.rusalts.ru   (или http://<IP>:8001 до включения домена)
#   DEBUG=true
#   CONTENT_APPROVAL_MODE=web          (проверять публикации вручную на staging)
#   QUEUE_REDIS_URL=redis://redis:6379/0  (свой volume — уже в compose)
```

## Запуск / остановка

```bash
# собрать и поднять
docker compose -f docker-compose.staging.yml --env-file .env.staging up -d --build

# логи
docker compose -f docker-compose.staging.yml logs -f web

# остановить (данные сохранятся)
docker compose -f docker-compose.staging.yml down
```

## Деплой на staging (GitHub Actions)

Workflow `deploy-staging.yml` (добавить): пуш в ветку `staging` → CI (pytest) →
rsync в `/opt/alt` → `docker compose -f docker-compose.staging.yml up -d --build`.

Правило: в `staging` попадает только то, что уже прошло проверку в PR/ветках;
перед релизом в main — staging проверяет Шеф.

## Чек-лист подъёма

- [x] `.env.staging` создан на сервере (копия прода + правки)
- [x] Стартовый запуск compose-staging → http://109.73.202.123:8001/health OK
- [x] DNS A-запись `staging` → 109.73.202.123 (сделал Шеф 01.09.2026)
- [x] certbot + nginx-конфиг → https://staging.rusalts.ru/health OK (01.09.2026)
- [ ] Workflow `deploy-staging.yml` в репо (CI + rsync + compose staging)
- [ ] Проверка: правка в staging-ветке → автообновление staging
