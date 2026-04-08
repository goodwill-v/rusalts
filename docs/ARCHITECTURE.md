# Архитектура АЛТ‑эксперт

## Сервисы и границы ответственности

### Backend (FastAPI)
- единый HTTP API для UI и каналов;
- RAG по Базе знаний (индексация/поиск);
- роутинг шаблонов по триггерам;
- интеграция с RouterAI (OpenAI‑совместимый API);
- логирование, метрики, трассировка.

### Frontend
- UI виджета, страниц консультационного сайта;
- адаптация под VK WebView/iframe;
- подготовка UI‑контрактов под будущие каналы.

### Parser
- планировщик 05:00 UTC;
- сбор данных из источников, нормализация, дедупликация;
- дифф и классификация изменений;
- обновление БЗ и формирование “change package”.

### Content
- генерация релизов/постов по change package;
- публикация на сайт (через API/файлы/CI) и в VK (через API);
- журнал “что ушло в прод”.

## Контракты (первый релиз)

### 1) Change package (Parser → Content / Backend)

Файл `data/changes/YYYY-MM-DD/change_package.json`:

```json
{
  "meta": {
    "generated_at_utc": "2026-04-06T05:10:00Z",
    "window": "daily",
    "parser_version": "0.1.0"
  },
  "sources": [
    {
      "id": "dev.max.ru/changelog",
      "url": "https://dev.max.ru/changelog",
      "fetched_at_utc": "2026-04-06T05:02:11Z",
      "etag": "..."
    }
  ],
  "items": [
    {
      "id": "max_changelog_2026-04-06_001",
      "category": "tech.api",
      "severity": "medium",
      "title": "Изменения Bot API: ...",
      "summary": "Коротко что изменилось.",
      "diff": {
        "type": "text",
        "before": "...",
        "after": "..."
      },
      "kb_targets": [
        {
          "kb_section": "MAX/Разработчикам/API_и_SDK",
          "kb_article_id": "max-api-changelog"
        }
      ],
      "links": [
        {"title": "Официальный changelog", "url": "https://dev.max.ru/changelog"}
      ],
      "tags": ["MAX", "Bot API", "changelog"]
    }
  ]
}
```

### 2) KB entry format (нормализованный)

Новый стандарт БЗ — “атомарные статьи” с метаданными:
- `id` (стабильный),
- `section_path` (иерархия),
- `updated_at_utc`,
- `sources[]` (ссылки),
- `text` (контент),
- `keywords[]` (для поиска),
- `legal_relevance` (если связано с законами/регуляторикой).

Подробно: `docs/KNOWLEDGE_BASE.md`.

### 3) Templates (триггеры → шаблоны)

Шаблоны хранятся отдельно от БЗ (как в примерах).
Триггеры: `knowledge_base/triggers.json`
Тексты: `templates/invitations_ru.json`

## Минимальные интерфейсы API (для фронта)

- `GET /widget` — страница виджета
- `POST /api/chat` — Q&A
- `GET /api/document-templates` — список файлов‑шаблонов
- `GET /api/files/document-templates/{filename}` — скачивание
- `GET /health` — healthcheck

Дальше (в план):
- `POST /api/ingest/change-package` (Parser → Backend)
- `GET /api/news` (Content/Backend → Frontend)
- `POST /api/publish/vk` (Content → VK)

