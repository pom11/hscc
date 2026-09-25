#!/usr/bin/env bash
# Wire the failure-escalation watcher into Hermes' native cron. Installs the
# runner under ~/.hermes/scripts/ and registers a Hermes cron job that runs it
# (--no-agent: the script IS the job). The runner itself sends a native macOS
# desktop notification for real escalations — hermes `--deliver desktop` is a
# silent no-op (resolves to no target), so the script notifies directly via
# hscc_daemon.desktop.send_desktop_notification(). An idle run stays silent
# and only real escalations notify.
# The runner reassigns repeatedly-failing tasks to the strong tier and flags a
# human when the strong tier also fails (deduped across runs). Idempotent:
# re-running refreshes the job.
#
# Usage:  scripts/install_escalate_watcher.sh [--uninstall]
set -euo pipefail

JOB="hscc-escalate-watcher"
SCHEDULE="${HSCC_ESCALATE_SCHEDULE:-*/15 * * * *}"   # every 15 min (cron spec)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_SCRIPTS="$HOME/.hermes/scripts"
HERMES="$HOME/.hermes/hermes-agent"
HERMES_BIN="$HERMES/venv/bin/hermes"
[ -x "$HERMES_BIN" ] || HERMES_BIN="hermes"

if [ "${1:-}" = "--uninstall" ]; then
  "$HERMES_BIN" cron remove "$JOB" 2>/dev/null || true
  rm -f "$HERMES_SCRIPTS/escalate_watcher_run.py"
  echo "removed cron job '$JOB' and the installed runner"
  exit 0
fi

mkdir -p "$HERMES_SCRIPTS"
cp "$SCRIPT_DIR/escalate_watcher_run.py" "$HERMES_SCRIPTS/escalate_watcher_run.py"

# Idempotent: drop any existing job of this name, then (re)create it.
# No --deliver: hermes `--deliver desktop` is a silent no-op; the runner sends
# its own native desktop notification, so output only lands in run history.
"$HERMES_BIN" cron remove "$JOB" 2>/dev/null || true
"$HERMES_BIN" cron create "$SCHEDULE" \
  --no-agent --script escalate_watcher_run.py \
  --name "$JOB"

echo "installed Hermes cron job '$JOB' (schedule: $SCHEDULE)"
echo "inspect:  $HERMES_BIN cron list"
echo "run now:  $HERMES_BIN cron run $JOB && $HERMES_BIN cron tick"
