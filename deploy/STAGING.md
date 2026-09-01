# Staging — черновик сайта для проверки (АЛТ / rusalts)

**Статус:** настраивается (01.09.2026). Решения Шефа: staging-контур поднимаем.

## Зачем staging

- Проверять новые версии кода **до** прода, не рискуя боевым сайтом.
- Агенты (L3) могут деплоить на staging свободно, на прод — только через релиз.

## Как устроено

| | Прод | Staging |
|---|------|---------|
| Ветка | `main` | `staging` |
| Порт | 8000 (через nginx → 443) | 8001 |
| Проект compose | `alt` (по умолчанию) | `alt-staging` |
| .env | `.env` | `.env.staging` |
| Данные (runtime) | `./data` | `./data_staging` |
| URL | https://rusalts.ru | http://<IP сервера>:8001 (пока без домена) |

## Файлы

- `docker-compose.staging.yml` — конфиг staging (порт 8001, свои volume, образ `alt-expert-web:staging`).
- `.env.staging` — копия `.env` прода с правками (см. ниже). **Секреты — только на сервере.**

## Настройка .env.staging (на сервере)

```bash
cd /opt/alt
cp .env .env.staging
# поправить:
#   PUBLIC_BASE_URL=http://<IP>:8001   (или staging.rusalts.ru, когда заведём домен)
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

- [ ] `.env.staging` создан на сервере (копия прода + правки)
- [ ] Стартовый запуск compose-staging → http://<IP>:8001/health OK
- [ ] Workflow `deploy-staging.yml` в репо (CI + rsync + compose staging)
- [ ] Проверка: правка в staging-ветке → автообновление staging
- [ ] (опц.) Поддомен staging.rusalts.ru + nginx-конфиг (когда решим)
