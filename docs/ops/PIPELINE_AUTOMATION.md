# Автоматическая работа Parser → Content → /publapprov/ → /news/ (без почты)

Этот документ объясняет “простыми словами”, как устроен автоматический конвейер новостей и что проверить, если на `/publapprov/` пусто.

## Коротко как работает

- **Parser worker** (парсер) читает список официальных источников и ищет изменения.
- Он сохраняет “пакет изменений” (`change package`) и отправляет событие в очередь.
- **Content worker** (контент) берёт изменения из очереди, готовит тексты (сайт + VK) и складывает **черновики** в очередь одобрения на странице `/publapprov/`.
- Редактор (вы) открывает `/publapprov/`, при необходимости правит текст и нажимает “Одобрить” — после этого новость появляется на `/news/` и (если настроено) публикуется в VK.

Важно: сейчас одобрение идёт **через веб‑страницу**, почта Chief отключена.

## Почему могло быть “Очередь пуста”

Самая частая причина в продакшене: внешний сервис LLM (RouterAI) временно недоступен.

Раньше это приводило к тому, что `content-worker` падал и **не создавал** черновик.
Теперь (после обновления) даже при ошибке RouterAI будет создан **черновик** с пометкой `Ошибка публикации`, чтобы процесс не останавливался.

## Ежедневный запуск в 05:00 UTC

Нужен “планировщик” на сервере. В репозитории есть systemd unit:

- `deploy/systemd/alt-pipeline.service`
- `deploy/systemd/alt-pipeline.timer`

Они **ставят задачу в очередь** через `docker compose exec ... enqueue_parser_content_once.py`.

### Установка на сервере (один раз)

На сервере (root):

```bash
cd /opt/alt
cp deploy/systemd/alt-pipeline.service /etc/systemd/system/alt-pipeline.service
cp deploy/systemd/alt-pipeline.timer /etc/systemd/system/alt-pipeline.timer
systemctl daemon-reload
systemctl enable --now alt-pipeline.timer
systemctl status alt-pipeline.timer --no-pager
```

Проверка, что таймер “виден”:

```bash
systemctl list-timers --all | grep alt-pipeline
```

### Разовый запуск “прямо сейчас” (тест)

```bash
cd /opt/alt
systemctl start alt-pipeline.service
```

или напрямую (без systemd):

```bash
docker compose exec -T web python /app/scripts/enqueue_parser_content_once.py --limit 3
```

## Что смотреть при проблемах

### 1) Очередь черновиков (должны появиться элементы)

```bash
curl -sS -u admin:20rusalt13 http://127.0.0.1:8000/api/content/queue | head
```

### 2) Логи воркеров

```bash
cd /opt/alt
docker compose logs --since=30m parser-worker content-worker
```

Если RouterAI недоступен, в элементах очереди будет `Ошибка публикации: ...` — это нормально: черновик создан, его можно вручную поправить и одобрить.

