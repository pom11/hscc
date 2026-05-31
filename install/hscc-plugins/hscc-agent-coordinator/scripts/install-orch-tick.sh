#!/usr/bin/env bash
# Install the HSCC orchestrator reconcile loop:
#   1. copy the detector to ~/.hermes/scripts/ (where Hermes cron --script looks)
#   2. register the hscc-orch-tick cron job (idempotent — skips if already present)
#   3. seed the autonomy gate to "off" (summarize-and-wait) if unset
#
# The detector + cron job + autonomy flag live OUTSIDE the plugin git root at
# runtime; this script is the version-controlled source that reconstitutes them.
#
# Env overrides:
#   DELIVER   delivery target (default telegram:0 — operator chat)
#   PROFILE   Hermes profile to run under (default: scheduler default)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_SCRIPTS="${HOME}/.hermes/scripts"
HSCC_HOME="${HSCC_HOME:-${HOME}/.hscc}"
DELIVER="${DELIVER:-telegram:0}"
JOB_NAME="hscc-orch-tick"

hermes_bin() {
  command -v hermes 2>/dev/null && return
  echo "${HOME}/.hermes/hermes-agent/venv/bin/hermes"
}
HERMES="$(hermes_bin)"

# 1. detector -> ~/.hermes/scripts/
mkdir -p "${HERMES_SCRIPTS}"
cp "${HERE}/hscc_orch_tick.py" "${HERMES_SCRIPTS}/hscc_orch_tick.py"
echo "[install] detector -> ${HERMES_SCRIPTS}/hscc_orch_tick.py"

# 2. cron job (idempotent)
if "${HERMES}" cron list 2>/dev/null | grep -q "Name:.*${JOB_NAME}"; then
  echo "[install] cron job '${JOB_NAME}' already present — skipping create"
else
  PROMPT="$(cat "${HERE}/orch-tick.prompt.txt")"
  ARGS=(--name "${JOB_NAME}" --deliver "${DELIVER}" --script hscc_orch_tick.py)
  [ -n "${PROFILE:-}" ] && ARGS+=(--profile "${PROFILE}")
  "${HERMES}" cron create "${ARGS[@]}" '* * * * *' "${PROMPT}"
  echo "[install] registered cron job '${JOB_NAME}' (every minute, deliver=${DELIVER})"
fi

# 3. autonomy gate -> off if unset
mkdir -p "${HSCC_HOME}"
if [ ! -f "${HSCC_HOME}/autonomy" ]; then
  printf 'off\n' > "${HSCC_HOME}/autonomy"
  echo "[install] seeded autonomy gate -> off (summarize-and-wait)"
else
  echo "[install] autonomy gate already set -> $(cat "${HSCC_HOME}/autonomy")"
fi

echo "[install] done."
