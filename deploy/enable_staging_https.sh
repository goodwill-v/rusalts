#!/bin/bash
# Включение HTTPS для staging.rusalts.ru
# Ждёт пропагацию DNS, выпускает сертификат, переписывает nginx-конфиг, проверяет.
set -u

DOMAIN="staging.rusalts.ru"
WEBROOT="/var/www/rusalts.ru/html"
CONF="/etc/nginx/sites-available/staging.rusalts.ru"
LOG="/root/.openclaw/workspace/АЛТ-промо/staging_https.log"

echo "$(date '+%F %T') === Старт включения HTTPS для $DOMAIN ===" >> "$LOG"

# 1. Ждём пропагацию DNS (до 15 минут, проверка каждые 30 сек через Google DNS)
ok=""
for i in $(seq 1 30); do
  if dig @8.8.8.8 +short "$DOMAIN" A 2>/dev/null | grep -q '^109\.73\.202\.123$'; then
    ok=1
    echo "$(date '+%F %T') DNS виден в интернете (попытка $i)" >> "$LOG"
    break
  fi
  sleep 30
done

if [ -z "$ok" ]; then
  echo "$(date '+%F %T') НЕ УСПЕЛИ: DNS не распространился за 15 минут" >> "$LOG"
  exit 1
fi

# 2. Выпуск сертификата (webroot, переиспользуем аккаунт rusalts.ru)
echo "$(date '+%F %T') Запуск certbot..." >> "$LOG"
certbot certonly --webroot -w "$WEBROOT" -d "$DOMAIN" \
  --non-interactive --agree-tos --no-eff-email --keep-until-expiring >> "$LOG" 2>&1

if [ ! -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ]; then
  echo "$(date '+%F %T') ОШИБКА: сертификат не выпущен" >> "$LOG"
  exit 1
fi
echo "$(date '+%F %T') Сертификат выпущен" >> "$LOG"

# 3. Полный nginx-конфиг: HTTP -> HTTPS, SSL-блок, прокси на 8001
cat > "$CONF" <<'NGINX'
server {
    listen 80;
    listen [::]:80;
    server_name staging.rusalts.ru;

    location ^~ /.well-known/acme-challenge/ {
        allow all;
        default_type "text/plain";
    }

    location / {
        return 301 https://$host$request_uri;
    }
}

server {
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name staging.rusalts.ru;

    ssl_certificate /etc/letsencrypt/live/staging.rusalts.ru/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/staging.rusalts.ru/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

    client_max_body_size 10m;

    location / {
        proxy_pass http://127.0.0.1:8001;
        include proxy_params;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_connect_timeout 60s;
        proxy_send_timeout 180s;
        proxy_read_timeout 180s;
    }
}
NGINX

# 4. Проверка и перезагрузка nginx
/usr/sbin/nginx -t >> "$LOG" 2>&1
if [ $? -ne 0 ]; then
  echo "$(date '+%F %T') ОШИБКА: nginx -t не прошёл" >> "$LOG"
  exit 1
fi
systemctl reload nginx
echo "$(date '+%F %T') nginx перезагружен" >> "$LOG"

# 5. Проверка HTTPS
code=$(curl -sS -m 20 -o /dev/null -w '%{http_code}' "https://$DOMAIN/health" 2>/dev/null)
echo "$(date '+%F %T') https://$DOMAIN/health -> HTTP $code" >> "$LOG"
echo "$(date '+%F %T') === ГОТОВО: https://$DOMAIN/ === " >> "$LOG"
