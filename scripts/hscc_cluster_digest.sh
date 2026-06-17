#!/bin/bash
# hscc_cluster_digest.sh — periodic cluster state summary.
# Emits always (digest, not watchdog). Routed to delivery target (telegram).

set -u
SPARKRUN="/Users/desac/.local/bin/sparkrun"

echo "*HSCC cluster digest — $(date '+%Y-%m-%d %H:%M')*"
echo ""

# Containers per host
STATUS=$("$SPARKRUN" cluster status 2>&1)
TOTAL=$(echo "$STATUS" | /usr/bin/grep -cE "^Job:" || true)
echo "Containers: $TOTAL job(s) across hosts"

# Endpoint health summary
HEALTHY=0
DOWN=()
for HOST in 10.0.0.244 10.0.0.246 10.0.0.247 10.0.0.248; do
  if /usr/bin/curl -s -f -m 3 "http://$HOST:8000/v1/models" >/dev/null 2>&1; then
    HEALTHY=$((HEALTHY+1))
  else
    DOWN+=("$HOST")
  fi
done
echo "Endpoints: $HEALTHY/4 healthy"
if [ ${#DOWN[@]} -gt 0 ]; then
  echo "  DOWN: ${DOWN[*]}"
fi

# Proxy state
PROXY_STATUS=$("$SPARKRUN" proxy status 2>&1 | /usr/bin/head -1 | /usr/bin/sed 's/Proxy status: //')
echo "Proxy: $PROXY_STATUS"

# Uptime per container
echo ""
echo "Uptime per job:"
echo "$STATUS" | /usr/bin/grep -E "^  solo " | /usr/bin/awk '{printf "  %s: %s %s %s\n", $2, $4, $5, $6}'
