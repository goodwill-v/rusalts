---
id: max-docs-map
section_path: MAX/Разработчикам/API_и_SDK
title: "Карта документации МАКС: где что искать (боты, каналы, бизнес)"
updated_at_utc: "2026-11-19T00:00:00Z"
keywords: ["МАКС", "MAX", "документация", "Bot API", "dev.max.ru", "business.max.ru", "SDK", "webhook", "long polling", "каналы"]
legal_relevance: "none"
sources:
  - title: "MAX Bot API: методы и события"
    url: "https://dev.max.ru/docs-api"
  - title: "MAX: события — Webhook и Long Polling"
    url: "https://dev.max.ru/help/events"
  - title: "GitHub: организация max-messenger (SDK, OpenAPI-схема)"
    url: "https://github.com/max-messenger"
  - title: "VK Реклама: продвижение в MAX"
    url: "https://ads.vk.ru/help/general/network/promotion_in_max"
---

# Карта документации МАКС

Куда смотреть, когда нужна документация мессенджера МАКС. Составлено 19.11.2026;
разделы проверены на доступность. Версия приложения на момент проверки: 26.38.1 (RuStore, 29.10.2026).

## 1. Официальные точки входа

| Ресурс | Что там |
|---|---|
| [dev.max.ru](https://dev.max.ru) | Портал для разработчиков и бизнеса («MAX для бизнеса»): регистрация ботов, платформа партнёров |
| [dev.max.ru/docs-api](https://dev.max.ru/docs-api) | **Документация Bot API**: методы (bots, chats, subscriptions, uploads, messages, comments), клавиатуры, вложения, сценарии |
| [dev.max.ru/help/events](https://dev.max.ru/help/events) | Справка «События»: Webhook vs Long Polling, требования к каждому |
| [business.max.ru](https://business.max.ru) | Платформа партнёров: каналы для бизнеса/самозанятых, чат-боты, токены ботов |
| [github.com/max-messenger](https://github.com/max-messenger) | Официальные SDK и схемы: TypeScript, Go, Python, OpenAPI-спецификация |
| [ads.vk.ru/help/general/network/promotion_in_max](https://ads.vk.ru/help/general/network/promotion_in_max) | Продвижение каналов/сайтов в МАКС через VK Рекламу |
| [rkn.gov.ru](https://rkn.gov.ru/press/news/news74982.htm) · [digital.gov.ru](https://digital.gov.ru/news/kanaly-v-mah-s-10-tys-podpischikov-mogut-zaregistrirovatsya-v-reestre-roskomnadzora) | Реестр каналов 10 000+ подписчиков (РКН), инструкции Минцифры |
| [vk.company/ru/press/releases](https://vk.company/ru/press/releases/) | Пресс-релизы VK — первичный источник новостей функций МАКС |

## 2. Bot API — базовые факты (актуальны на 19.11.2026)

- **Домен:** `platform-api2.max.ru` (не `platform-api.max.ru`). Запросы без актуального сертификата в доверенных падают по SSL.
- **Авторизация:** заголовок `Authorization: <access_token>`; передача токена через query-параметры больше не поддерживается.
- **Токен бота:** business.max.ru → «Чат-боты → ⋮ → Настройки», либо мини-апп/бот «MAX для бизнеса» → «Получить токен».
- **События — два способа, одновременно нельзя:**
  - **Webhook** (для production): `POST /subscriptions` с URL; endpoint обязан отвечать HTTP 200; при 8 часах без успешных ответов бот **автоматически отписывается**. Требования: публичный HTTPS-сервер со статичным IP; с 25.05.2026 самоподписанные сертификаты и HTTP не принимаются — нужны доверенные ЦС (включая сертификаты Минцифры).
  - **Long Polling** (разработка/тестирование): `GET /updates`; внешний сервер со статичным IP не нужен; при высокой интенсивности — задержки и очереди соединений.
- **Ключевые методы:** `POST /messages` (отправка), `POST /answers` (ответ на callback-кнопку), inline-клавиатуры, `POST /uploads` (медиа), `GET/POST /messages/{messageId}/comments` (комментарии к постам канала), `GET/PUT/DELETE /chats/{chatId}/pin` (закрепление), `PATCH /me/commands` (команды бота).
- **Изменения API 2026 (важно при обновлении старого кода):**
  - с июня 2026 `GET /chats` не поддерживается — вместо него подписка на события;
  - с 09.09.2026 ограничен `POST /chats/{chatId}/members`;
  - код ошибок читать по полю `code` (например `not.found`), не по `message`.
- **SDK:** `max-bot-api-client-ts` (TypeScript), `max-bot-api-client-go` (Go), `max-botapi-python` (Python), `api-schema` (OpenAPI-спецификация); есть шаблон мини-аппы `max-app`.

## 3. Живые кейсы АЛТ (код в монорепо)

| Кейс | Где | Что показывает |
|---|---|---|
| ВежПом — бот мониторинга 24/7 | `bot/vezpom/vezpom_webhook.py` | Long Polling в проде: `GET /updates?limit=5&timeout=25` на `platform-api2.max.ru`, обработка команд, отправка сообщений |
| Уведомления о лидах | `app/max_notify.py` | Серверный POST `https://platform-api2.max.ru/messages?chat_id=...` с заголовком `Authorization: <token>`; сбой отправки не ломает основной сценарий |
| НейроСотрудник (бот с GigaChat) | `/opt/max-bot/bot.py` (вне репо) | Long Polling + выбор ассистента; токены в `.env` |

Операционный нюанс: для исходящих HTTPS-запросов к API из контура сервера проверка цепочки сертификата НУЦ Минцифры может требовать индивидуальной настройки доверенных ЦС (в `app/max_notify.py` используется `verify=False` — осознанно, с комментарием; для webhook-серверов это неактуально, там требования к сертификату задаёт сам МАКС).

## 4. Правило актуальности

API и функции МАКС обновляются ежемесячно (релизы 26.xx). Перед работой с новым функционалом:
1. сверить методы с [dev.max.ru/docs-api](https://dev.max.ru/docs-api) (первоисточник);
2. проверить пресс-релизы VK за последние месяцы (новые функции: опросы в каналах, донаты, статистика — появлялись именно так);
3. в этой БЗ изменения платформы отслеживает парсер (`src-*` статьи секций `MAX/`), обновляйте карту только при смене структуры официальных ресурсов.
