# Проверка RouterAI и моделей (smoke test)

Если в логах появляется ошибка вида `RouterAI request failed` или в `/publapprov/` не появляются материалы из-за сбоя генерации, сначала нужно проверить:

- доступность RouterAI (сеть/URL)
- корректность API‑ключа
- корректность **ID моделей** в `.env`

## Быстрый тест на сервере (рекомендуется)

На сервере в `/opt/alt`:

```bash
cd /opt/alt
docker compose exec -T web python /app/scripts/test_routerai_models.py
```

Скрипт:
- берёт `ROUTERAI_BASE_URL` и `ROUTERAI_API_KEY` из `.env`;
- проверяет модели: `BACKEND_*`, `PARSER_*`, `CONTENT_*`, `ROUTERAI_*`;
- печатает список `[OK] / [FAIL]` по каждой модели.

## Если есть ошибки

Типовые причины:

- **Неверный `ROUTERAI_BASE_URL`**: должен указывать на OpenAI‑compatible API.  
  В проекте поддерживаются варианты вида `https://routerai.ru/api` и `https://routerai.ru/api/v1`.
- **Неверный ID модели**: RouterAI может не поддерживать конкретный идентификатор, даже если он выглядит “правильно”.
- **Проблемы сети**: DNS/фаервол/временная недоступность.

### Что править

- `.env`: `ROUTERAI_BASE_URL`, `ROUTERAI_API_KEY`, `*_MODEL_*`
- при необходимости — код (улучшение таймаутов/повторных попыток)

## Где смотреть диагностические детали

Мы улучшили клиент `app/routerai.py`: теперь в `RouterAI request failed` выводится:
- тип ошибки (`ConnectTimeout`, `ReadTimeout`, …)
- URL запроса
- `repr()` исключения (чтобы не было “пустых” ошибок)

