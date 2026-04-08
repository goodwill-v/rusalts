# Агент 1: Backend (API, RAG, RouterAI, интеграции)

## Цели

- дать стабильный API для фронта и каналов;
- отвечать на вопросы на основе БЗ + системного промпта;
- выдавать шаблоны по триггерам (отдельно от БЗ);
- обеспечить безопасность, логирование, тестирование.

## Краткий бриф (для раздачи роли “Backend”)

- **Системный промпт**: `АЛЬТЕРНАТИВА_АЛТбот/ALT_sist.prompt.md`
- **База знаний (исходник)**: `АЛЬТЕРНАТИВА_АЛТбот/ALT_Knowledge_Base.md` → канонический формат: `knowledge_base/articles/*` (см. `docs/KNOWLEDGE_BASE.md`)
- **Шаблоны**: `templates/*.json`, триггеры: `knowledge_base/triggers.json`
- **LLM**: RouterAI (см. `docs/ROUTERAI.md`), обязательный учёт usage/cost

MVP‑цели:
- RAG/поиск по структурированной БЗ (особый приоритет `MAX/Регуляторика_и_комплаенс/*`)
- маршрутизация шаблонов по `knowledge_base/triggers.json` + подстановка переменных
- интеграция RouterAI в `POST /api/chat` + безопасный фолбэк
- JSON‑логи с `request_id` и usage/cost

## Входы

- База знаний: `knowledge_base/`
- Шаблоны: `templates/` и `knowledge_base/triggers.json`
- Системный промпт общения: `ALT_sist.prompt.md` (см. примеры)
- Переменные окружения: `.env` (RouterAI/VK/домен)

## Выходы

- API для UI и внешних каналов
- JSON‑логи (включая usage/cost по LLM)

## Основные задачи (MVP)

### 1) RAG по БЗ
- Индексация статей БЗ по секциям и ключевым словам.
- Приоритет секции `MAX/Регуляторика_и_комплаенс/*`.
- Возврат выдержек + источников + даты актуализации.

### 2) Подбор шаблонов по триггерам
- Match по `knowledge_base/triggers.json`.
- Подстановка переменных из `templates/*.json`.
- Логирование: `trigger_id`, `template_key`, `timestamp`, `request_id`.

### 3) RouterAI интеграция
- OpenAI‑compatible client.
- Политика минимальной достаточности (см. `docs/ROUTERAI.md`).
- Фолбэк: если LLM недоступна — ответ из БЗ + предложение обратиться в поддержку.

### 4) Безопасность и VK
- CSP `frame-ancestors` и CORS уже заложены.
- В перспективе: проверка подписи VK launch params на бэкенде.

## API контракты (расширение)

- `POST /api/chat`: принимает текст + optional контекст (канал, user_id)
- `GET /api/kb/search?q=`: внутренний поиск (для отладки/админки)
- `POST /api/ingest/change-package`: принять пакет изменений от Parser

## Definition of Done

- тесты API проходят
- логи содержат `request_id` и (если LLM) `tokens_in/out`
- БЗ выдаёт источники и дату актуализации

