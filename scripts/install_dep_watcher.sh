#!/usr/bin/env bash
# Wire the dependency-bump watcher into Hermes' native cron. Installs the
# poller under ~/.hermes/scripts/ and registers a daily Hermes cron job that
# runs it (--no-agent: the script IS the job, stdout delivered verbatim). The
# poller turns dep-bump PRs (opened by the check-runtime-deps GitHub Action,
# labelled needs-cluster-check with the owner as reviewer) into kanban
# verification cards. Idempotent: re-running refreshes the job.
#
# Usage:  scripts/install_dep_watcher.sh [--uninstall]
set -euo pipefail

JOB="hscc-dep-watcher"
SCHEDULE="${HSCC_DEP_WATCHER_SCHEDULE:-0 8 * * *}"   # daily 08:00 (cron spec)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_SCRIPTS="$HOME/.hermes/scripts"
HERMES="$HOME/.hermes/hermes-agent"
HERMES_BIN="$HERMES/venv/bin/hermes"
[ -x "$HERMES_BIN" ] || HERMES_BIN="hermes"

if [ "${1:-}" = "--uninstall" ]; then
  "$HERMES_BIN" cron remove "$JOB" 2>/dev/null || true
  rm -f "$HERMES_SCRIPTS/dep_pr_watcher.py"
  echo "removed cron job '$JOB' and the installed poller"
  exit 0
fi

mkdir -p "$HERMES_SCRIPTS"
cp "$SCRIPT_DIR/dep_pr_watcher.py" "$HERMES_SCRIPTS/dep_pr_watcher.py"

# Idempotent: drop any existing job of this name, then (re)create it.
"$HERMES_BIN" cron remove "$JOB" 2>/dev/null || true
"$HERMES_BIN" cron create "$SCHEDULE" \
  --no-agent --script dep_pr_watcher.py \
  --name "$JOB" --deliver telegram

echo "installed Hermes cron job '$JOB' (schedule: $SCHEDULE)"
echo "inspect:  $HERMES_BIN cron list"
echo "run now:  $HERMES_BIN cron run $JOB && $HERMES_BIN cron tick"
