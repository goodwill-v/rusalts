# Разовый запуск цепочки Parser → Content (очередь Redis)

Цель: **не ждать** systemd/cron и один раз прогнать полный конвейер через воркеры: парсинг источников, обновление БЗ, затем подготовка (и при необходимости публикация) контента.

## Как устроено

1. В стрим `alt:parser:jobs` (или значение `QUEUE_STREAM_PARSER_JOBS`) попадает сообщение типа **`parser.run`**.
2. **parser-worker** забирает задачу, выполняет `run_once`, при необходимости кладёт **`content.from_change_package`** в `alt:content:jobs`.
3. **content-worker** генерирует текст (RouterAI) и в режиме `local_autoapprove` публикует на сайт; иначе оставляет материал на согласовании.

Это тот же путь, что у HTTP **`POST /api/parser/enqueue`**.

Важно: таймер **`deploy/systemd/alt-parser.timer`** на сервере запускает **`python -m app.parser_agent.cli`** в venv на хосте. Этот путь **не использует Redis** и **не вызывает content-worker**. Для проверки именно очереди используйте скрипт ниже или `enqueue` API.

## Условия

- Запущены контейнеры **`redis`**, **`parser-worker`**, **`content-worker`** (и обычно **`web`**).
- В `.env` на сервере заданы **`QUEUE_REDIS_URL`** (в Docker по умолчанию совпадает с `redis://redis:6379/0`) и ключи RouterAI для Content.

## Разовый запуск на сервере (`/opt/alt`)

Из каталога с `docker-compose.yml`:

```bash
cd /opt/alt
docker compose ps redis parser-worker content-worker web
docker compose exec -T web python /app/scripts/enqueue_parser_content_once.py
```

Ограничить число источников (отладка):

```bash
docker compose exec -T web python /app/scripts/enqueue_parser_content_once.py --limit 5
```

## Альтернатива: HTTP (если порт 8000 доступен с хоста)

```bash
curl -sS -X POST "http://127.0.0.1:8000/api/parser/enqueue" \
  -H "Content-Type: application/json" \
  -d '{}'
```

С машины, где API за reverse proxy, подставьте свой базовый URL.

## Как убедиться, что отработало

```bash
docker compose logs --tail=80 parser-worker content-worker
```

В логах ищите JSON-события вроде `parser_run_complete`, `content_published_from_queue` или `content_queued_pending_approval` (зависит от `CONTENT_APPROVAL_MODE`).

Артефакты парсера и контента лежат под `./data` (смонтировано с хоста в проде).

## Локально

Поднять стек с очередью и воркерами (как в репозитории), затем:

```bash
docker compose exec -T web python /app/scripts/enqueue_parser_content_once.py --limit 3
```

или из venv на машине разработчика с `QUEUE_REDIS_URL`, указывающим на доступный Redis.
