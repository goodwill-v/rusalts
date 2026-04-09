# Деплой через GitHub (Docker Compose + SSH)

## Идея

Пуш в ветку `main` → GitHub Actions:
- запускает тесты (workflow `CI`)
- затем (workflow `Deploy`) синхронизирует код на сервер по SSH и поднимает контейнеры через `docker compose`.

## Что должно быть на сервере

- Установлены Docker и **Docker Compose v2** (команда `docker compose`):
  - `docker --version`
  - `docker compose version`
- `docker-compose` (v1) **не используем** (устаревший бинарник).
- Создан каталог деплоя, например `/opt/alt` (или другой).
- В каталоге деплоя лежит файл `.env` (секреты **только** на сервере).

## Настройка GitHub Secrets

В репозитории GitHub → Settings → Secrets and variables → Actions → **New repository secret**:

- `SSH_HOST` — домен/IP сервера
- `SSH_PORT` — обычно `22`
- `SSH_USER` — пользователь (например `root` или `deploy`)
- `SSH_PRIVATE_KEY` — приватный ключ для доступа (без пароля, или используйте отдельный deploy key)
- `DEPLOY_PATH` — каталог на сервере (например `/opt/alt`)

## Первый запуск

1. На сервере создайте каталог:
   ```bash
   sudo mkdir -p /opt/alt
   sudo chown -R $USER:$USER /opt/alt
   ```
2. На сервере положите `.env` (не коммитить).
3. Сделайте первый push в `main` — CI и Deploy запустятся автоматически.

## Локальный запуск через Compose

```bash
docker compose build
docker compose up
```

