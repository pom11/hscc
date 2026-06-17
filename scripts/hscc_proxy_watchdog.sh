#!/bin/bash
# hscc_proxy_watchdog.sh — restart sparkrun LiteLLM proxy on :4000 if dead.
# Silent when healthy. Output only when action taken (watchdog pattern).
# Run via: hermes cron create '5m' --no-agent --script hscc_proxy_watchdog.sh

set -u
PROXY_URL="http://localhost:4000/v1/models"
SPARKRUN="/Users/desac/.local/bin/sparkrun"
CLUSTER="hscc"
PORT=4000

if /usr/bin/curl -s -f -m 4 "$PROXY_URL" >/dev/null 2>&1; then
  exit 0
fi

echo "[$(date -Iseconds)] sparkrun proxy :$PORT unreachable. Restarting via cluster=$CLUSTER..."
OUT=$("$SPARKRUN" proxy start --cluster "$CLUSTER" --port "$PORT" 2>&1)
RC=$?

if [ $RC -ne 0 ]; then
  echo "FAILED (rc=$RC):"
  echo "$OUT"
  exit 1
fi

sleep 3
if /usr/bin/curl -s -f -m 4 "$PROXY_URL" >/dev/null 2>&1; then
  echo "Proxy restarted OK. 4 endpoints registered."
else
  echo "WARN: restart attempt completed but health check still failing."
  echo "$OUT"
fi
