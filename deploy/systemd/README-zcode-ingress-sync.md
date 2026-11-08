# Живой ingress-токен zcode для /zcode (установка: Алик/Шеф)

zcode-server v3.17.1 генерирует ingress-токен **заново при каждом старте**
(`createIngressToken`: `randomBytes(32)` → rename в `run/ingress-token`).
Штатного способа зафиксировать токен у сервера нет. Поэтому токен доставляется
в контейнеры вживую:

```
/root/.zcode/server/run/ingress-token   (меняет zcode-server, файл подменяется rename'ом)
  → zcode-ingress-sync.path (systemd, событие изменения)
  → zcode-ingress-sync.service (cat > /etc/zcode-web/ingress-token, inode стабилен)
  → docker bind-mount: /etc/zcode-web/ingress-token:/run/secrets/zcode_ingress_token:ro
  → app/zcode_rpc.py читает файл при каждом подключении (перечитывает при реконнекте)
```

Статичная `ZCODE_INGRESS_TOKEN` в `.env` остаётся только резервом (если файла нет).

## Установка (один раз)

```bash
# 1) первичная копия (до этого compose-файл монтирует путь — файл должен существовать,
#    иначе docker создаст на его месте каталог)
mkdir -p /etc/zcode-web
cat /root/.zcode/server/run/ingress-token > /etc/zcode-web/ingress-token
chmod 600 /etc/zcode-web/ingress-token

# 2) юниты
cp /opt/alt/deploy/systemd/zcode-ingress-sync.{path,service} /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now zcode-ingress-sync.path

# 3) проверка
systemctl status zcode-ingress-sync.path
cat /etc/zcode-web/ingress-token | wc -c   # 43 байта + перевод строки

# 4) пересоздать контейнеры, чтобы примонтировался файл
cd /opt/alt && docker compose up -d --force-recreate web
cd /opt/alt && docker compose -f docker-compose.staging.yml --env-file .env.staging up -d --force-recreate web
```

## Проверка ротации (после следующего рестарта zcode-server Алик/Шефом)

```bash
sleep 2; diff <(cat /root/.zcode/server/run/ingress-token) /etc/zcode-web/ingress-token && echo SYNC-OK
```

Веб-чат `/zcode` продолжает работать без правки `.env`: при следующем
подключении приложение прочитает обновлённый файл (кэша токена нет).

## Почему не «просто примонтировать run/ingress-token»

1. Сервер подменяет файл через `rename` → bind-mount одиночного файла прибивает
   старый inode и молча устаревает.
2. Монтирование каталога `run/` целиком раскрыло бы контейнеру `control.sock`
   (управляющий сокет zcode-server, включая команду shutdown) — лишняя
   поверхность атаки.
