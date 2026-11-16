#!/bin/bash
# Автосинк порта zcode-server → nginx zcode-bridge (решение задачи Шефа 16.11).
# zcode-server выбирает СЛУЧАЙНЫЙ порт при каждом старте (options.port ?? 0),
# фикс-порта не предусмотрено → следим за /root/.zcode/server/run/status.json.
# Триггер: systemd .path unit на изменение status.json.
# Лог: /opt/alt_docs/ALT_ZAI_devops/zcode_port_sync.log
#
# ИСТОЧНИК ПРАВДЫ: монорепо rusalts, deploy/systemd/zcode-port-sync.{sh,path,service}
# (установленная на хосте копия — рабочая; правки скрипта/юнитов — только через PR).
set -euo pipefail
LOG=/opt/alt_docs/ALT_ZAI_devops/zcode_port_sync.log
CONF=/etc/nginx/sites-available/zcode-bridge
STAMP=$(TZ=Asia/Yekaterinburg date '+%F %H:%M:%S %Z')

PORT=$(python3 -c "import json;print(json.load(open('/root/.zcode/server/run/status.json')).get('port') or '')") || PORT=""
[ -n "$PORT" ] || { echo "$STAMP ERROR: status.json без порта" >> "$LOG"; exit 1; }

OLD=$(grep -oE "127\.0\.0\.1:[0-9]+" "$CONF" | head -1 | cut -d: -f2)
if [ "$OLD" = "$PORT" ]; then
  echo "$STAMP порт не изменился ($PORT) — пропущено" >> "$LOG"
  exit 0
fi

sed -i "s/127\.0\.0\.1:${OLD}/127.0.0.1:${PORT}/g" "$CONF"
if nginx -t 2>/dev/null; then
  systemctl reload nginx
  echo "$STAMP OK: порт ${OLD} → ${PORT}, nginx перезагружен" >> "$LOG"
else
  # откат при битом конфиге (не должно случаться, но страховка)
  sed -i "s/127\.0\.0\.1:${PORT}/127.0.0.1:${OLD}/g" "$CONF"
  nginx -t && systemctl reload nginx || true
  echo "$STAMP ERROR: nginx -t не прошёл, откат к ${OLD}" >> "$LOG"
  exit 1
fi
