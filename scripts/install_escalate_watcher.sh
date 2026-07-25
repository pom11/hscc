#!/usr/bin/env bash
# Wire the failure-escalation watcher into Hermes' native cron. Installs the
# runner under ~/.hermes/scripts/ and registers a Hermes cron job that runs it
# (--no-agent: the script IS the job; --deliver telegram: its stdout goes to the
# HSCC group, so an idle run stays silent and only real escalations message you).
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
"$HERMES_BIN" cron remove "$JOB" 2>/dev/null || true
"$HERMES_BIN" cron create "$SCHEDULE" \
  --no-agent --script escalate_watcher_run.py \
  --name "$JOB" --deliver telegram

echo "installed Hermes cron job '$JOB' (schedule: $SCHEDULE)"
echo "inspect:  $HERMES_BIN cron list"
echo "run now:  $HERMES_BIN cron run $JOB && $HERMES_BIN cron tick"
