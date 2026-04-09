# .env: чек‑лист недостающих настроек (особенно для публикаций)

> Секреты не должны попадать в репозиторий. Используйте `.env` только локально/на сервере.

## RouterAI

- `ROUTERAI_BASE_URL`
- `ROUTERAI_API_KEY`
- (рекомендуется добавить) `ROUTERAI_CHAT_MODEL`
- (рекомендуется добавить) `ROUTERAI_CHEAP_MODEL`
- (рекомендуется добавить) `ROUTERAI_REASONING_MODEL`
- (рекомендуется добавить) `ROUTERAI_EMBEDDINGS_MODEL`

## Модели по агентам (prod main → heavy)

- Backend: `BACKEND_MODEL_MAIN`, `BACKEND_MODEL_HEAVY`
- Parser: `PARSER_MODEL_MAIN`, `PARSER_MODEL_HEAVY`
- Content: `CONTENT_MODEL_MAIN`, `CONTENT_MODEL_HEAVY`

## Queue / inter-service messaging (Redis)

- `QUEUE_REDIS_URL`
- `QUEUE_STREAM_PARSER_JOBS`, `QUEUE_STREAM_CONTENT_JOBS`
- `QUEUE_GROUP_PARSER`, `QUEUE_GROUP_CONTENT`

## VK (виджет/мини‑приложение)

Уже есть:
- `VK_APP_ID`
- `VK_SECURE_KEY`
- `VK_SERVICE_TOKEN` (серверный доступ к VK API)

Для **публикации постов** в сообщество обычно потребуется дополнительно:
- `VK_GROUP_ID` (id сообщества)
- `VK_API_VERSION` (например `5.199`)
- `VK_WALL_ACCESS_TOKEN` (или иной токен с правом `wall`)

## Content (согласование по почте Chief)

Нужно добавить для отправки на согласование и приёма ответов:

- `CHIEF_EMAIL_TO` (по ТЗ: `v.devops@yandex.ru`)
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_TLS`
- `IMAP_HOST`, `IMAP_PORT`, `IMAP_USER`, `IMAP_PASSWORD`, `IMAP_FOLDER`

## Публичный URL и безопасность

- `PUBLIC_BASE_URL`
- `CORS_ORIGINS`
- `FRAME_ANCESTORS`
- `MAX_UPLOAD_BYTES`
- `DEBUG`

