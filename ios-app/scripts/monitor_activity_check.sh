#!/bin/bash
# monitor_activity_check.sh — prove the HSCC Monitor Live Activity's data path
# against the LIVE API (real models + real MonitorContent derivation compiled
# verbatim, real /v1/cluster/monitor + /v1/daemon/host JSON decoded).
#
# Usage: scripts/monitor_activity_check.sh   (requires ~/.hscc/api-token +
# a reachable API; host from WIDGET_LIVE_HOST or the first host lsof reports on
# :8788).
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# Resolve the live API host to a plain IP (never print the tailnet name).
HOST="${WIDGET_LIVE_HOST:-}"
if [ -z "$HOST" ]; then
  HOST=$(lsof -iTCP -sTCP:LISTEN -P 2>/dev/null | grep ':8788' | awk '{print $9}' | sed 's/:8788//' | head -1)
  # Turn a tailnet name into an IP via host(1).
  IPHOST=$(host "$HOST" 2>/dev/null | awk '/has address/{print $NF; exit}')
  [ -n "$IPHOST" ] && HOST="$IPHOST"
fi
echo "live host resolved (redacted)" >&2

WIDGET_LIVE_HOST="$HOST" WIDGET_LIVE_PORT="${WIDGET_LIVE_PORT:-8788}" \
  python3 scripts/monitor_activity_check/run.py
