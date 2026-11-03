# Купец — Telegram-бот поиска по маркетплейсам

**t.me/KupetBot** — находит и сравнивает товары на Ozon, Wildberries и
Яндекс.Маркете, ранжирует по SCORE (цена/продавец/отзывы/доставка),
объясняет выбор через RouterAI.

## Почему поиск через Firecrawl (важно)

Прямые запросы к Ozon/WB с сервера АЛТ блокируются антиботом (403 fab_chlg,
WAF режет датацентр-IP) — Composer API мёртв с сентября 2026, scrapе карточек
невозможен физически. Живой канал один: **поисковые сниппеты Firecrawl** —
один OR-запрос `site:ozon.ru OR site:wildberries.ru OR site:market.yandex.ru`
(2 кредита за поиск). Цены есть в сниппетах ~40% случаев (WB/ЯМ), у Ozon
почти никогда — товары без цены показываются с пометкой «цена по ссылке».
Tavily как резервный провайдер тоже блокируется WAF с этого сервера (проверено
03.11.2026) — ключ в .env на будущее/другие площадки.

## Лимиты и алерты

- Счётчик кредитов — SQLite (`data/kupets_state.db`), переживает рестарты.
- Пороги 80/95/100% лимита (`FIRECRAWL_MONTHLY_CREDITS`, free = 1000) →
  алерт Шефу на почту (`REPORT_EMAIL_TO`) и в МАКС (`MAX_BOT_TOKEN` +
  `MAX_ALERT_CHAT_ID`, тот же бот, что шлёт лиды). Анти-спам: один уровень
  не чаще `ALERT_COOLDOWN_HOURS` (12 ч).
- HTTP 402 от Firecrawl → поиск останавливается, пользователь получает
  вежливое пояснение (не «Ничего не нашёл»).

## Кэш

Ответы поиска кэшируются в SQLite на 3 ч (`CACHE_TTL_SEARCH_SECONDS`) —
повторный запрос не тратит кредиты. Ключ = SHA256(текст+фильтры).

## Деплой (как ВежПом)

1. Секреты: `/opt/kupets/.env` из `.env.example` (НЕ в git).
2. Юнит: `cp deploy/systemd/kupets-bot.service /etc/systemd/system/`.
3. Дальше автоматом: push в main → CI rsync `bot/kupets/` → `/opt/kupets/`,
   pip install, `systemctl restart kupets-bot` (шаг в `.github/workflows/deploy.yml`).

## Структура

- `bot/` — aiogram-хендлеры, форматы сообщений; `services/` — пайплайн,
  кредиты/алерты, почта; `parser/` — firecrawl-адаптер (основной) и
  отключённый composer; `engine/` — фильтры/скоринг/guard болтовни;
  `ai/explainer.py` — RouterAI-пояснения топ-3.
