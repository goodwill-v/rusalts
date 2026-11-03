# ВежПом — бот мониторинга в МАКС

**ВежПом** — ИИ-агент в мессенджере МАКС: мониторинг сайтов и серверов 24/7
(аптайм, SSL-сроки, резервные копии), алерты, подписка через витрину АЛТ.
Продукт: `rusalts.ru/techologis/p/vezhpom/`. Дорожная карта:
`docs/ВежПом_дорожная_карта.md`, архитектура развития:
`docs/ВежПом_архитектура_блок1_SSL_дайджест.md`.

## Схема работы

```
MAX-пользователь ──/monitor──► бот создаёт монитор в Uptime Kuma (kuma.py, Socket.IO)
Uptime Kuma ──алерт (HTTP POST)──► вебхук-сервер бота (:4321) ──► сообщение в МАКС
Подписка: витрина rusalts.ru (vitrina_client) — источник правды, касса YooKassa
```

## Файлы

| Файл | Назначение |
|---|---|
| `vezpom_webhook.py` | Бот v3.1: MAX long-polling команд + вебхук-сервер для событий Kuma + подписка |
| `kuma.py` | Клиент Uptime Kuma (Socket.IO 1.23.x): создание/удаление мониторов, пульсации |
| `db.py` | SQLite: users, payments, monitors (владелец = `chat<id>` в имени монитора) |
| `vitrina_client.py` | Клиент витрины: тариф, подписка, платежи, возвраты (X-API-Key, fail-open) |

## Запуск (прод: сервер АЛТ)

Systemd-юнит: `deploy/systemd/vezpom.service` → активен как `/etc/systemd/system/vezpom.service`,
конфиг с секретами — `/opt/vezpom/.env` (по образцу `.env.example`, в git не хранится).

```bash
# локальная проверка
python3 -c "import db, kuma, vitrina_client, vezpom_webhook"  # импорты на месте
```

## Деплой из CI

Пуш в `main` → GitHub Actions (`deploy.yml`): сайт деплоится docker compose,
затем шаг «Deploy VeZhPom bot» rsync-ит `bot/vezpom/` → `/opt/vezpom/`
(рантайм-файлы `.env`, `*.db`, `*.log`, `backup*` исключены) и перезапускает
`vezpom.service` с health-проверкой `GET :4321/health`.

## История переноса

- До 03.11.2026 код жил в `/opt/vezpom/` на сервере (вне git — нарушение
  AGENTS.md §1). В монорепо перенесены актуальные модули; на сервере остались
  только артефакты рантайма и история (`backup*/`, `patches/`, устаревший
  `yookassa.py` — не импортируется, путь платежей теперь через витрину).

## Развитие

План работ и правила анти-шума — `docs/ВежПом_дорожная_карта.md` (сроки SSL/
бэкапов/доменов, ежедневный «светофор», еженедельный отчёт).
