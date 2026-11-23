#!/usr/bin/env bash
# Wire the failure-escalation watcher into Hermes' native cron. Generates a stub
# under <bound home>/scripts/ and registers a Hermes cron job that runs it
# (--no-agent: the script IS the job). The stub holds NO copy of the runner — it
# runs the repo's file — so the code the job executes is the code in git.
# The runner itself sends a native macOS desktop notification for real
# escalations — hermes `--deliver desktop` is a silent no-op (resolves to no
# target), so the script notifies directly via
# hscc_daemon.desktop.send_desktop_notification(). An idle run stays silent and
# only real escalations notify.
# The runner reassigns repeatedly-failing tasks to the strong tier and flags a
# human when the strong tier also fails (deduped across runs). Idempotent:
# re-running refreshes the job.
#
# WHY A GENERATED STUB AND NOT A COPY (t_accbc5da, removing the class t_95eb9f15
# made visible in the dep watcher). This installer used to `cp` the runner into
# the bound home, and a second copy of the code on a host is a second copy that
# can go stale — same shape, same silence, one watcher over. There is nothing to
# compare any more, because there is nothing to go stale: `git pull` moves the
# job. (hscc_daemon/cron_watchers.py still compares a leftover copy for exactly
# one release: an operator who has not re-run this installer yet is still running
# the file the old shape left behind, and the `copy:` line of --status below is
# what tells them to re-run it.)
#
# THE STUB IS A .py, AND IT HANDS OFF IN-PROCESS — both are load-bearing:
#   Hermes picks a cron script's interpreter by EXTENSION and deliberately
#   ignores the shebang (cron/scheduler_script.py::_script_argv: .sh/.bash ->
#   bash, .py -> Hermes' own dependency venv). escalate_watcher_run.py needs that
#   venv (it imports hermes_cli and hscc_daemon), so the job has to stay a .py —
#   a .sh stub like install_alone_sweep_gate.sh's would put a PATH python3 in
#   front of the runner. And the handoff must not spawn a child: on a
#   managed-store install those imports are reachable only inside the process
#   Hermes bootstrapped (hermes-agent #123044), so exec-ing the interpreter again
#   would lose them. runpy in the same process keeps the interpreter, the
#   sys.path Hermes set up, and the target's exit code.
#
# Usage:  scripts/install_escalate_watcher.sh [--uninstall | --status]
set -euo pipefail

JOB="hscc-escalate-watcher"
# Named after the cron job (install_alone_sweep_gate.sh's convention), NOT after
# the repo script: the deployed file is a generated stub and an operator who cats
# it must see that at a glance. It is also why the retired copy gets removed
# rather than overwritten — nothing named `escalate_watcher_run.py` belongs under
# <bound home>/scripts any more.
WRAPPER="hscc-escalate-watcher.py"
WATCHER="escalate_watcher_run.py"
SCHEDULE="${HSCC_ESCALATE_SCHEDULE:-*/15 * * * *}"   # every 15 min (cron spec)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
# DRIFT RULE (t_3aadd2c0 / t_e286c4b9): `hermes cron create --script NAME` resolves
# NAME only against <bound home>/scripts, and profiles never share files — the bound
# home is $HERMES_HOME (default ~/.hermes). A hard-coded ~/.hermes/scripts here while
# the job bound the INVOKING profile's home produced a job whose script could never
# be found (hscc-dep-watcher failed 17 consecutive daily runs that way). The stub
# destination and the cron binding must resolve from the SAME home — derive both
# here, and pass only the FILENAME to `cron create`.
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
STREAM="escalate_watcher"
# The path the stub bakes, and how --status reads it back out. Keep the
# `# HSCC-TARGET:` spelling identical across the watcher installers.
TARGET="$SCRIPT_DIR/$WATCHER"

if [ "${1:-}" = "--uninstall" ]; then
  "$HERMES_BIN" cron remove "$JOB" 2>/dev/null || true
  rm -f "$HERMES_SCRIPTS/$WRAPPER"
  # And the file the RETIRED copy shape left behind: the job no longer runs it, so
  # leaving it is a decoy a future registration could pick up.
  rm -f "$HERMES_SCRIPTS/$WATCHER"
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
cw.mark_removed("escalate_watcher")
PY
  echo "removed cron job '$JOB', $HERMES_SCRIPTS/$WRAPPER and any leftover $WATCHER"
  exit 0
fi

# --status: same shape as install_dep_watcher.sh --status (and
# install_alone_sweep_gate.sh's before it). `target:` is the wrapper shape's OWN
# failure mode (t_accbc5da): the stub bakes an absolute repo path at install
# time, so moving or deleting the checkout leaves it dangling and the job fails
# on its next tick. `copy:` stays (t_95eb9f15) for hosts still running the copy
# shape — that line is what tells an operator to re-run this installer, and it
# settles on `not-a-copy` once they have. Both comparison lines come from the
# CHECKOUT's hscc_daemon/cron_watchers.py so installer, bootstrap doctor and
# `hscc verify` cannot disagree about what "alive" or "the same file" means.
_wrapper_target() {
  sed -n 's/^# HSCC-TARGET: \(.*\)$/\1/p' "$1" 2>/dev/null | tail -1 || true
}

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
    || echo "           NOT REGISTERED — run: scripts/install_escalate_watcher.sh"
  echo "wrapper:   $HERMES_SCRIPTS/$WRAPPER"
  if [ ! -f "$HERMES_SCRIPTS/$WRAPPER" ]; then
    echo "           MISSING — the job fails on its next tick (re-run this installer)"
  else
    baked="$(_wrapper_target "$HERMES_SCRIPTS/$WRAPPER")"
    if [ -z "$baked" ]; then
      echo "           NOT GENERATED by this installer (no HSCC-TARGET marker)"
    elif [ -f "$baked" ]; then
      echo "           runs $baked"
    else
      echo "           DANGLING — it runs $baked, which does not exist: the"
      echo "           checkout moved or was deleted. Re-run this installer."
    fi
  fi
  if [ -e "$HERMES_SCRIPTS/$WATCHER" ]; then
    echo "leftover:  $HERMES_SCRIPTS/$WATCHER — this installer does not copy any"
    echo "           more; re-run it to drop the file and run the repo's copy"
  fi
  echo "repo copy: $SCRIPT_DIR/$WATCHER"
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

[ -z "${1:-}" ] || { echo "install_escalate_watcher.sh: unknown argument '$1' (expected --uninstall or --status)" >&2; exit 2; }

[ -f "$TARGET" ] \
  || { echo "no watcher at $TARGET — is this checkout complete?" >&2; exit 2; }

mkdir -p "$HERMES_SCRIPTS"
if [ -e "$HERMES_SCRIPTS/$WATCHER" ]; then
  # The retired copy shape's file. Deleting it is what closes the class on this
  # host: while it sits there, a stale second copy of the runner is one
  # `--script escalate_watcher_run.py` away from running again.
  rm -f "$HERMES_SCRIPTS/$WATCHER"
  echo "removed the deployed copy $HERMES_SCRIPTS/$WATCHER (this installer does not copy any more)"
fi

# Generated stub: the resolved repo path baked at install time, then run the
# repo's file. Same shape as install_alone_sweep_gate.sh's wrapper, .py for the
# interpreter reason in the header. Written to a .tmp and moved so a failure
# mid-write cannot leave a half-written stub that a 15-minute tick would run.
cat > "$HERMES_SCRIPTS/$WRAPPER.tmp" <<EOF
#!/usr/bin/env python3
# GENERATED by $REPO/scripts/install_escalate_watcher.sh — do not edit.
# HSCC-TARGET: $TARGET
# Hermes cron job '$JOB' runs this stub. It holds NO copy of the runner: it runs
# the file named in HSCC-TARGET above, so \`git pull\` in that checkout is what
# changes what this job does and there is no second copy that can go stale
# (t_accbc5da — the drift a copy used to leave behind is what t_95eb9f15 caught).
# Re-run scripts/install_escalate_watcher.sh to re-point this stub (a moved
# checkout makes the baked path dangle) or to change the schedule.
#
# The handoff is IN-PROCESS on purpose: Hermes picked this process's interpreter
# (and, on a managed-store install, its sys.path) for a .py cron script, and
# exec-ing a fresh interpreter would drop hermes_cli / hscc_daemon — see the
# header of $REPO/scripts/install_escalate_watcher.sh.
import os
import runpy
import sys

TARGET = "$TARGET"

if not os.path.isfile(TARGET):
    sys.stderr.write(
        "hscc-escalate-watcher: the checkout this stub was installed from is gone:\\n"
        "  %s\\n"
        "  re-run: <that checkout>/scripts/install_escalate_watcher.sh\\n" % TARGET)
    raise SystemExit(2)

# Mirror \`python <script>\`: sys.path[0] is the script's own directory.
sys.path.insert(0, os.path.dirname(TARGET))
sys.argv[0] = TARGET
runpy.run_path(TARGET, run_name="__main__")
EOF
mv "$HERMES_SCRIPTS/$WRAPPER.tmp" "$HERMES_SCRIPTS/$WRAPPER"
chmod 0755 "$HERMES_SCRIPTS/$WRAPPER"

# Idempotent: drop any existing job of this name, then (re)create it.
# No --deliver: hermes `--deliver desktop` is a silent no-op; the runner sends
# its own native desktop notification, so output only lands in run history.
"$HERMES_BIN" cron remove "$JOB" 2>/dev/null || true
"$HERMES_BIN" cron create "$SCHEDULE" \
  --no-agent --script "$WRAPPER" \
  --name "$JOB"

echo "installed Hermes cron job '$JOB' (schedule: $SCHEDULE)"
echo "  wrapper : $HERMES_SCRIPTS/$WRAPPER -> $TARGET"
echo "  repo    : $REPO (baked; re-run this installer after moving the checkout)"
echo "inspect:  $HERMES_BIN cron list"
echo "status:   scripts/install_escalate_watcher.sh --status"
echo "run now:  $HERMES_BIN cron run $JOB && $HERMES_BIN cron tick"
