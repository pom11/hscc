#!/bin/bash
# hscc_nas_watchdog.sh — periodic NAS health probe.
# Watches for the known failure modes from project_nas_export_fragility:
#   - QNAP unreachable (network/qShield ban)
#   - Mac NAS mount lost (share reboots while this host stays up)
#   - QNAP export wiped (empty /etc/exports)
# Silent when healthy.
#
# Scheduling note: the daemon's own `nas` check stream is the primary detector
# and the `nas-down` trigger rule is what alerts on it. This script is the
# standalone/manual probe — it needs no daemon.

set -u

# NO address is baked in: this repo is public, so a literal LAN/tailnet address
# in a tracked file is a leak (and the scrub that removed the last one left a
# placeholder behind that silently probed a host which does not exist). The
# operator supplies it; absent, the reachability probe is SKIPPED rather than
# reported as a failure against a host we were never told.
NAS_HOST="${HSCC_NAS_HOST:-}"
MAC_MOUNT="${HSCC_NAS_MOUNT:-/Volumes/NAS}"
PROBLEMS=()
SKIPPED=()

# 1. Reachability. `ping -W` is MILLISECONDS on macOS but SECONDS on Linux, so
#    the portable per-probe bound is -t (BSD) / -w (GNU); -c 1 -t 2 is the form
#    that means "2 seconds" on the macOS host this runs on.
if [ -n "$NAS_HOST" ]; then
  if ! /sbin/ping -c 1 -t 2 "$NAS_HOST" >/dev/null 2>&1; then
    PROBLEMS+=("NAS $NAS_HOST does not respond to ICMP — check qShield ban or network")
  fi
else
  SKIPPED+=("reachability (set HSCC_NAS_HOST to enable)")
fi

# 2. Local mount. A directory that exists proves nothing — an unmounted
#    mountpoint is still a listable empty dir, and `df` on it reports the LOCAL
#    boot disk (that is how a 12-day outage went unnoticed in Aug 2026). Require
#    a real mount AND a readable payload, since a dead NFS handle still appears
#    in the mount table while every read fails.
if /sbin/mount | /usr/bin/grep -q " ${MAC_MOUNT} "; then
  if ! /bin/ls "$MAC_MOUNT/hub" >/dev/null 2>&1; then
    PROBLEMS+=("$MAC_MOUNT is mounted but $MAC_MOUNT/hub is not readable — stale NFS handle, remount needed")
  fi
elif [ -d "$MAC_MOUNT" ]; then
  PROBLEMS+=("$MAC_MOUNT exists but is NOT a mount — export or mount lost (check NAS uptime first: a reboot means only the clients need remounting)")
else
  PROBLEMS+=("mount point $MAC_MOUNT missing entirely")
fi

# 3. NFS export probe via showmount is deliberately NOT attempted: the QNAP
#    serves the NFSv4 pseudo-root and does not expose the v3 MOUNT protocol, so
#    showmount returns nothing even when perfectly healthy. The real probe is a
#    fresh mount attempt, which is too invasive for a periodic check — step 2
#    covers the symptom that matters.

if [ ${#PROBLEMS[@]} -eq 0 ]; then
  exit 0
fi

echo "[$(date -Iseconds)] HSCC NAS watchdog: ${#PROBLEMS[@]} issue(s):"
for p in "${PROBLEMS[@]}"; do
  echo "  - $p"
done
if [ ${#SKIPPED[@]} -gt 0 ]; then
  for s in "${SKIPPED[@]}"; do
    echo "  (skipped: $s)"
  done
fi
echo ""
echo "Refer to project_nas_export_fragility for remediation."
exit 1
