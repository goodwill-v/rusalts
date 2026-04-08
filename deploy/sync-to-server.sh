#!/usr/bin/env bash
# Обновление тестового деплоя на rusalts.ru (запускать из корня репозитория на своей машине).
set -euo pipefail
HOST="${DEPLOY_HOST:-root@109.73.202.123}"
rsync -avz \
  --exclude '.venv' \
  --exclude '.git' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '.cursor' \
  --exclude '.env' \
  ./ "${HOST}:/opt/alt/"
rsync -avz ./deploy/public/ "${HOST}:/var/www/rusalts.ru/html/"
ssh "${HOST}" 'chown -R www-data:www-data /opt/alt /var/www/rusalts.ru/html; systemctl restart alt-consultant; systemctl --no-pager -l status alt-consultant | head -12'
