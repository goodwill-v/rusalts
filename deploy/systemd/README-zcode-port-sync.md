# Автосинк порта zcode-server → nginx-мост (zcode-port-sync)

## Зачем

zcode-server при **каждом старте** выбирает **случайный порт** (`options.port ?? 0`,
фикс-опции в коде v3.17.1 нет — проверено по бандлу 16.11) и одновременно меняет
ingress-токен (см. `README-zcode-ingress-sync.md`). Контейнеры ходят в zcode через
nginx-мост `/etc/nginx/sites-available/zcode-bridge` (172.17.0.1:8082 → 127.0.0.1:PORT),
поэтому при рестарте zcode-server мост обязан «догнать» новый порт — иначе `/zcode`
ломается до ручного вмешательства.

Цепочка (порт):

```
zcode-server стартует → новый случайный порт → /root/.zcode/server/run/status.json (поле port)
  → systemd zcode-port-sync.path (событие изменения status.json)
  → zcode-port-sync.service → zcode-port-sync.sh:
      сравнить порт из status.json с портом в конфиге моста
      → совпал: пропустить; нет: sed в zcode-bridge → nginx -t → reload
      → при ошибке nginx -t: откат правки (страховка)
  → мост снова смотрит на актуальный порт, /zcode работает без паузы
```

Лог: `/opt/alt_docs/ALT_ZAI_devops/zcode_port_sync.log`.

Токен синхронизируется соседним юнитом `zcode-ingress-sync` (его README).

## Файлы

| Файл (в репо: `deploy/systemd/`) | Устанавливается в | Роль |
|---|---|---|
| `zcode-port-sync.sh` | `/opt/alt_docs/ALT_ZAI_devops/zcode_port_sync.sh` | логика синка (сравнение/sed/проверка/откат/лог) |
| `zcode-port-sync.path` | `/etc/systemd/system/` | наблюдатель за `status.json` |
| `zcode-port-sync.service` | `/etc/systemd/system/` | oneshot-исполнитель |

**Источник правды — репо.** Установленная на хосте копия рабочая; правки
скрипта/юнитов — только через PR (правило AGENTS.md §1).

## Установка на хост (один раз)

```bash
# 1) скрипт — туда, где его ждёт .service
cp /opt/alt/deploy/systemd/zcode-port-sync.sh /opt/alt_docs/ALT_ZAI_devops/zcode_port_sync.sh
chmod +x /opt/alt_docs/ALT_ZAI_devops/zcode_port_sync.sh

# 2) юниты
cp /opt/alt/deploy/systemd/zcode-port-sync.{path,service} /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now zcode-port-sync.path

# 3) проверка
systemctl status zcode-port-sync.path          # active (waiting)
systemctl start zcode-port-sync.service        # ручной прогон
tail -3 /opt/alt_docs/ALT_ZAI_devops/zcode_port_sync.log
# ожидаемо: «порт не изменился (N) — пропущено» либо «OK: порт X → Y»
```

Примечание: `/opt/alt` пересоздаётся деплоем (rsync + `docker compose down/up`),
поэтому файл из репо попадает на хост при каждом деплое автоматически —
`git pull` на хосте не нужен, путь `/opt/alt/deploy/systemd/...` актуален.

## Проверка живой ротации

```bash
# Следующий рестарт zcode-server (Алик/Шеф) менять порт и токен — автоматика
# догоняет обе части: .path-юниты срабатывают в течение секунды.
# Ручная симуляция смены порта (правка status.json СЕРВИСОМ НЕ ДЕЛАЕТСЯ —
# только чтение! для теста достаточно убедиться, что лог растёт при рестарте):
tail -f /opt/alt_docs/ALT_ZAI_devops/zcode_port_sync.log
# после рестарта zcode-server: строка «OK: порт X → Y, nginx перезагружен»
curl -s -o /dev/null -w '%{http_code}\n' https://rusalts.ru/zcode   # 200
```

## Почему не «просто захардкодить порт»

- Порт случайный по дизайну сервера (безопасный автоподбор свободного).
- Правки status.json в сторону фиксированного значения сервер игнорирует
  (порт выбирается при bind).
- Синк через nginx-мост прозрачен для приложения: `ZCODE_UPSTREAM_HTTP_URL`
  (172.17.0.1:8082) не меняется никогда, переменные окружения стабильны.

## Прочим агентам/инструментам (ходящим напрямую, минуя мост)

- порт читать из `/root/.zcode/server/run/status.json` (поле `port`) при каждом подключении;
- токен — из `/root/.zcode/server/run/ingress-token`;
- НЕ хардкодить ни то, ни другое (образец: `/tmp/kor_send.py` Кора, наш `app/zcode_rpc.py`).
