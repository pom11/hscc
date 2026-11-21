#!/usr/bin/env bash
# Wire the dependency-bump watcher into Hermes' native cron. Installs the
# poller under ~/.hermes/scripts/ and registers a daily Hermes cron job that
# runs it (--no-agent: the script IS the job). The poller turns dep-bump PRs
# (opened by the check-runtime-deps GitHub Action, labelled needs-cluster-check
# with the owner as reviewer) into kanban verification cards; the poller itself
# sends a native desktop notification when it creates a card — hermes
# `--deliver desktop` is a silent no-op. Idempotent: re-running refreshes the
# job.
#
# Usage:  scripts/install_dep_watcher.sh [--uninstall | --status]
set -euo pipefail

JOB="hscc-dep-watcher"
SCHEDULE="${HSCC_DEP_WATCHER_SCHEDULE:-0 8 * * *}"   # daily 08:00 (cron spec)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# DRIFT RULE (t_3aadd2c0): `hermes cron create --script NAME` resolves NAME only
# against <bound home>/scripts, and profiles never share files — the bound home
# is $HERMES_HOME (default ~/.hermes). The old installer hard-coded the shared
# ~/.hermes/scripts while the job bound to the INVOKING profile's home, so
# installing from a profile produced a job whose script could never be found:
# hscc-dep-watcher failed 17 consecutive daily runs that way. The install dir
# and the cron binding must resolve from the SAME home — derive both here.
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERMES_SCRIPTS="$HERMES_HOME/scripts"
HERMES="$HOME/.hermes/hermes-agent"
HERMES_BIN="$HERMES/venv/bin/hermes"
[ -x "$HERMES_BIN" ] || HERMES_BIN="hermes"
# Liveness marker helper lives in the deployed hscc_daemon payload (GAP-B-4).
HERMES_PLUGINS="$HERMES_HOME/plugins"
[ -d "$HERMES_PLUGINS" ] || HERMES_PLUGINS="$HOME/.hermes/plugins"
# The hscc_daemon/cron_watchers.py stream this job stamps (the doctor's row and
# this --status read the same roster row, so they cannot disagree).
STREAM="dep_pr_watcher"

if [ "${1:-}" = "--uninstall" ]; then
  "$HERMES_BIN" cron remove "$JOB" 2>/dev/null || true
  rm -f "$HERMES_SCRIPTS/dep_pr_watcher.py"
  # Drop the liveness install marker too (GAP-B-4), or the doctor keeps
  # reporting a watcher nobody installed as "installed but never ran".
  python3 - <<'PY' 2>/dev/null || true
import os, sys
p = os.path.expanduser("~/.hermes/plugins")
if os.path.isdir(p) and p not in sys.path:
    sys.path.insert(0, p)
try:
    from hscc_daemon import cron_watchers as cw
except ImportError:
    raise SystemExit(0)
cw.mark_removed("dep_pr_watcher")
PY
  echo "removed cron job '$JOB' and the installed poller"
  exit 0
fi

# --status: what to check first, in the shape install_alone_sweep_gate.sh
# --status already established. Both lines come from the CHECKOUT's
# hscc_daemon/cron_watchers.py, so this installer, `python3
# hscc-bootstrap/doctor.py` and `hscc verify` cannot disagree about what
# "alive" or "the same file" means.
#
# The `copy:` line is the reason this exists (t_95eb9f15). This installer `cp`s
# the poller into the bound home, which leaves a SECOND copy of the code on the
# host, and nothing had ever compared the two: the deployed copy here predated
# GAP-B-4 stamping, so the job ran fine every morning and the doctor reported
# `dep_pr_watcher: not configured` — a missing stamp blamed on an operator who
# had installed the watcher, when the install itself was the stale part. The
# `alone_sweep_gate` job has this problem structurally removed (it generates a
# one-line wrapper that execs the repo's gate); until these two move to that
# shape, the comparison has to be said out loud.
_watcher_status_lines() {
  python3 - "$SCRIPT_DIR/.." "$STREAM" <<'PY' 2>/dev/null
import importlib.util, os, sys
repo, stream = sys.argv[1], sys.argv[2]
spec = importlib.util.spec_from_file_location(
    "hscc_cron_watchers", os.path.join(repo, "hscc_daemon", "cron_watchers.py"))
if spec is None or spec.loader is None:
    raise SystemExit(1)
cw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cw)
d = cw.deployed_copy_drift(stream)
print(f'copy:     {d["status"]}: {d["detail"]}')
v = cw.verdict(stream, copy=d)
print(f'liveness: {v["status"]}: {v["detail"]}')
PY
}

if [ "${1:-}" = "--status" ]; then
  echo "job:       $JOB"
  "$HERMES_BIN" cron list 2>/dev/null | grep -F "$JOB" \
    || echo "           NOT REGISTERED — run: scripts/install_dep_watcher.sh"
  echo "installs:  $HERMES_SCRIPTS/dep_pr_watcher.py"
  if [ -f "$HERMES_SCRIPTS/dep_pr_watcher.py" ]; then
    echo "           present"
  else
    echo "           MISSING in this home — the job fails on its next tick if this"
    echo "           home is the bound one (the copy: line checks every candidate home)"
  fi
  echo "repo copy: $SCRIPT_DIR/dep_pr_watcher.py"
  lines="$(_watcher_status_lines || true)"
  if [ -n "$lines" ]; then
    printf '%s\n' "$lines"
  else
    # A check that could not run must not read as an absent problem.
    echo "copy:      UNKNOWN — hscc_daemon/cron_watchers.py not loadable from $SCRIPT_DIR/.."
    echo "liveness:  UNKNOWN — same reason"
  fi
  exit 0
fi

[ -z "${1:-}" ] || { echo "install_dep_watcher.sh: unknown argument '$1' (expected --uninstall or --status)" >&2; exit 2; }

mkdir -p "$HERMES_SCRIPTS"
cp "$SCRIPT_DIR/dep_pr_watcher.py" "$HERMES_SCRIPTS/dep_pr_watcher.py"

# Idempotent: drop any existing job of this name, then (re)create it.
# No --deliver: hermes `--deliver desktop` is a silent no-op; the poller sends
# its own native desktop notification on card creation.
"$HERMES_BIN" cron remove "$JOB" 2>/dev/null || true
"$HERMES_BIN" cron create "$SCHEDULE" \
  --no-agent --script dep_pr_watcher.py \
  --name "$JOB"

# Liveness marker (GAP-B-4): the doctor's staleness check must be able to tell
# "installed, but the cron job never completed a pass" (the 17-consecutive-
# failures incident, t_3aadd2c0) from "never configured". Written through
# hscc_daemon/cron_watchers.py so the file schema stays in ONE place. Best-
# effort: a missing runtime module must not fail the install.
python3 - "$HERMES_PLUGINS" "$SCHEDULE" <<'PY' || echo "note: could not write the liveness install marker (doctor will report this watcher as never-installed)"
import os, sys
plugins, schedule = sys.argv[1], sys.argv[2]
if os.path.isdir(plugins) and plugins not in sys.path:
    sys.path.insert(0, plugins)
try:
    from hscc_daemon import cron_watchers as cw
except ImportError:
    sys.exit(1)
sys.exit(0 if cw.mark_installed("dep_pr_watcher", schedule) else 1)
PY

echo "installed Hermes cron job '$JOB' (schedule: $SCHEDULE)"
echo "inspect:  $HERMES_BIN cron list"
echo "run now:  $HERMES_BIN cron run $JOB && $HERMES_BIN cron tick"
