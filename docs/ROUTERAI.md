# RouterAI: подключение, модели, учёт затрат

## Подключение

RouterAI используется как OpenAI‑совместимый шлюз.
Базовый URL и ключ уже предусмотрены в `.env`:
- `ROUTERAI_BASE_URL`
- `ROUTERAI_API_KEY`

Рекомендуемые дополнительные переменные:
- `ROUTERAI_CHAT_MODEL` — модель “по умолчанию” для ответов
- `ROUTERAI_CHEAP_MODEL` — дешёвая модель для классификации/рутинных задач
- `ROUTERAI_REASONING_MODEL` — модель для сложных кейсов (редко)
- `ROUTERAI_EMBEDDINGS_MODEL` — для эмбеддингов (если включаем RAG)

## Конкретные идентификаторы моделей (сверено по RouterAI `/models`)

Рекомендуемый набор по принципу “минимальной достаточности”:

- **`ROUTERAI_CHEAP_MODEL`**: `google/gemini-2.5-flash-lite`
- **`ROUTERAI_CHAT_MODEL`**: `google/gemini-2.5-flash`
- **`ROUTERAI_REASONING_MODEL`**: `anthropic/claude-opus-4.6`
- **`ROUTERAI_EMBEDDINGS_MODEL`**: `baai/bge-m3`

Эти значения уже добавлены в `.env.example`.

## Политика “минимальной достаточности”

Правило:
- сначала дешёвые модели: классификация запроса, поиск по БЗ, подбор шаблонов;
- дорогие модели только если:
  - нет ответа в БЗ,
  - есть сложная компоновка источников,
  - высокий риск ошибки (регуляторика/безопасность) → повышаем качество, но с цитированием источников.

## Таблица учёта расходов на токены (шаблон)

Файл‑шаблон для ведения учёта: `docs/token_costs_template.csv`

Рекомендуемый формат строк:
- `date_utc`
- `service` (backend/parser/content)
- `model`
- `purpose` (chat/rag/classify/summarize/publish)
- `input_tokens`
- `output_tokens`
- `cost_usd`
- `request_id`

## Требование к логированию затрат

Backend/Parser/Content должны логировать в JSONL:
- модель,
- токены input/output,
- request_id,
- user_id (если есть) или анонимный идентификатор,
- стоимость (если RouterAI отдаёт usage/cost; иначе считаем по справочнику цен).

