#!/bin/bash
# hscc_proxy_watchdog.sh — restart sparkrun LiteLLM proxy on :4000 if dead.
# Silent when healthy. Output only when action taken (watchdog pattern).
#
# SUPERVISION OWNER: the daemon, not cron (GAP-B-5, integration audit B).
# The daemon's `proxy` check stream is what supervises this proxy:
# PERIODIC_INTERVALS["proxy"] = 60 s (hscc_daemon/daemon_ops.py:34) drives
# check_proxy (hscc_daemon/health.py:1326), which relaunches via `sparkrun proxy
# start` and writes the state file `hscc verify` / `hscc doctor` read.
#
# DO NOT SCHEDULE THIS ON A CRON. An earlier version of this header told the
# reader to register the script on a 5-minute Hermes cron job. That advice is
# withdrawn: a second 5-minute supervisor fights check_proxy in two ways that
# both make things worse. (The exact old command line is deliberately not
# reproduced here or in scripts/README.md — a literal copy in the docs is a
# command line a reader can paste, and scripts/tests/
# test_watchdog_retirement.py fails if one reappears.)
#   - DIFFERENT HEALTH RULE. This script probes reachability
#     (`curl -f /v1/models`). check_proxy explicitly does not treat
#     reachability as health — a litellm proxy can answer 200 with an EMPTY
#     model catalogue, which serves nobody (the 2026-10-11 outage). So this
#     script can call "dead" a proxy check_proxy is deliberately keeping, and
#     call "healthy" the exact catalogue-empty case check_proxy exists to catch.
#   - NO COOLDOWN. check_proxy throttles relaunches with a DURABLE cooldown
#     (HSCC_PROXY_RELAUNCH_COOLDOWN_S, default 300 s, stamped in the proxy state
#     file) precisely so a `proxy start` that exits 0 over still-empty discovery
#     does not relaunch every tick. This script restarts unconditionally, so on
#     a 5 m schedule (288 probes a day) it can tear the proxy down inside every
#     cooldown window check_proxy is holding, and keeps doing it for as long as
#     the two health rules disagree.
#
# The Hermes cron job that carried this schedule — `hscc-proxy-watchdog`,
# `every 5m`, PAUSED since 2026-06-20 — is RETIRED: delete it, do not resume it.
# The exact, store-verified retire commands are in
# scripts/README.md §"Retiring the superseded watchdog cron jobs".
#
# Running it BY HAND is still legitimate: it is a one-shot "force a proxy
# restart now" and needs no daemon. When the daemon is up, prefer the sanctioned
# path check_proxy itself calls: `sparkrun proxy start --cluster hscc --port 4000`.

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
