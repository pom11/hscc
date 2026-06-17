#!/bin/bash
# hscc_nas_watchdog.sh — periodic NAS health probe.
# Watches for the known failure modes from project_nas_export_fragility:
#   - QNAP unreachable (network/qShield ban)
#   - Mac NAS mount lost
#   - Spark-side NAS handle stale (detected via worker_health which depends on NAS)
# Silent when healthy.

set -u
NAS_HOST="10.0.0.249"
MAC_MOUNT="/Volumes/NAS"
PROBES=()
PROBLEMS=()

# 1. ping NAS
if ! /sbin/ping -c 1 -W 2 "$NAS_HOST" >/dev/null 2>&1; then
  PROBLEMS+=("NAS $NAS_HOST does not respond to ICMP — check qShield ban or network")
fi

# 2. Mac local mount: directory exists + listable
if [ -d "$MAC_MOUNT" ]; then
  if ! /bin/ls "$MAC_MOUNT/hub" >/dev/null 2>&1; then
    PROBLEMS+=("Mac mount $MAC_MOUNT/hub not listable — remount may be needed")
  fi
else
  PROBLEMS+=("Mac mount point $MAC_MOUNT missing")
fi

# 3. NFS export probe via showmount
# QNAP doesn't expose v3 MOUNT — showmount returning nothing is normal.
# Real test = a fresh mount attempt; too invasive for cron. Skip here, rely on Spark vLLM health.

if [ ${#PROBLEMS[@]} -eq 0 ]; then
  exit 0
fi

echo "[$(date -Iseconds)] HSCC NAS watchdog: ${#PROBLEMS[@]} issue(s):"
for p in "${PROBLEMS[@]}"; do
  echo "  - $p"
done
echo ""
echo "Refer to project_nas_export_fragility for remediation."
