#!/usr/bin/env bash
# Restart the hscc daemon through HSCC's own lifecycle (`stop` then `start`)
# and VERIFY the process actually turned over by comparing PIDs.
#
# Why: bootstrap's daemon-setup step (launchd/systemd-setup.sh) leaves an
# ALREADY-RUNNING daemon alone, so freshly-copied plugin code was never loaded.
# On the live host the daemon kept running the process started eight days
# earlier and the freshly-installed autodown loop silently never loaded — that
# cost real debugging time twice. This script fixes it: stop -> start -> confirm
# the PID changed.
#
# Restart goes through the daemon's OWN commands (`hscc stop` / `hscc start`),
# never a raw kill, so all normal shutdown/supervision paths (graceful SIGTERM,
# pid-file cleanup, launchd/systemd keep-alive semantics) are respected. It is
# non-fatal to the caller (bootstrap): a failed restart must not abort an
# otherwise-good install, so we report a distinct exit code per failure mode
# instead of dying.
#
# How the daemon CLI is invoked — the crux of this fix. Running
# hscc_daemon/hscc.py by bare path fails instantly with `ModuleNotFoundError:
# No module named 'hscc_daemon'`, because the script imports its own package and
# a bare script path does not put its dir on sys.path. Both `stop` and `start`
# therefore died before doing anything, and a `>/dev/null 2>&1` hid the
# traceback — the daemon just kept running. We now invoke the CLI like the
# daemon and operator actually do:
#   B (preferred) the installed `hscc` console script on PATH (the same entry
#     point the running daemon was launched with), used whenever we are NOT
#     under a test stub (no HSCC_CMD/HSCC_PYBIN env override) and `hscc`
#     resolves.
#   A (fallback)  $HSCC_PYBIN $HSCC_CMD with PYTHONPATH pointed at the package
#     root (parent of the dir holding HSCC_CMD) so a bare script path can still
#     import its own package. This honours the HSCC_CMD/HSCC_PYBIN env
#     overrides, which is how the tests stub the daemon.
#
# We also capture stop/start output instead of swallowing it: when the restart
# fails we surface the real reason (e.g. the ModuleNotFoundError traceback) in
# the outcome line rather than only "PID unchanged". Being loud is the whole
# point of this step.
#
# Safety / autodown (see task): restarting briefly suspends watchdog
# supervision, but the new daemon's startup path (daemon_ops.run_daemon_loop)
# re-runs autodown.resume_from_restart, which re-asserts the intentional block
# when state=="down" and finishes the wake when state=="waking" — so a
# bootstrap-time restart can never strand a down/waking fleet. This script does
# not alter autodown's armed/disarmed state and never touches
# ~/.hscc/autodown.json or ~/.hscc/watchdog-block.json.
#
# Usage: restart_daemon.sh [--skip]
#   --skip   do nothing (no stop/start, no PID check). bootstrap passes this
#            when it runs with --skip-daemon so no restart is attempted.
#
# Env (all optional; tests override them to stub the daemon):
#   HSCC_CMD        path to the daemon CLI script (default
#                   ~/.hermes/plugins/hscc_daemon/hscc.py); when SET we always
#                   run it via $HSCC_PYBIN (path A) so tests can stub it
#   HSCC_PID_FILE   daemon pid file        (default ~/.hscc/daemon.pid)
#   HSCC_PYBIN      python interpreter     (default hermes venv, else python3)
#
# Prints a single outcome line (no ✓/⚠ prefix — the caller owns ok/warn) and
# exits:
#   0  restart verified (PID changed), OR fresh start (was not running), OR skipped
#   2  PID unchanged after restart — daemon left running is the OLD process
#   3  daemon did not come back after restart, or hscc command missing
set -uo pipefail

SKIP=false
[ "${1:-}" = "--skip" ] && SKIP=true

# Record whether the user (or tests) explicitly set these, BEFORE defaulting
# them. ${X+x} is non-empty only if X is present in the environment.
HSCC_CMD_SET=0;   [ -n "${HSCC_CMD+x}" ]   && HSCC_CMD_SET=1
HSCC_PYBIN_SET=0; [ -n "${HSCC_PYBIN+x}" ] && HSCC_PYBIN_SET=1

HSCC_PYBIN="${HSCC_PYBIN:-$HOME/.hermes/hermes-agent/venv/bin/python}"
[ -x "$HSCC_PYBIN" ] || HSCC_PYBIN="python3"
HSCC_CMD="${HSCC_CMD:-$HOME/.hermes/plugins/hscc_daemon/hscc.py}"
HSCC_PID_FILE="${HSCC_PID_FILE:-$HOME/.hscc/daemon.pid}"
POLL_ATTEMPTS="${POLL_ATTEMPTS:-5}"
POLL_INTERVAL="${POLL_INTERVAL:-1}"

# Package root = parent of the dir holding HSCC_CMD. For the default this is
# ~/.hermes/plugins; for a test stub in a tmp dir it is that tmp dir. Putting it
# on PYTHONPATH is what lets a bare script path import its own package — the
# direct fix for the ModuleNotFoundError.
HSCC_PLUGIN_ROOT="$(CDPATH= cd -- "$(dirname -- "$HSCC_CMD")/.." 2>/dev/null && pwd)"

daemon_pid() {
  [ -f "$HSCC_PID_FILE" ] && cat "$HSCC_PID_FILE" 2>/dev/null || echo ""
}

# True when we can (and should) use the installed `hscc` console script (path
# B): no env override forcing a stub, and `hscc` resolves on PATH.
using_console_script() {
  [ "$HSCC_CMD_SET" -eq 0 ] && [ "$HSCC_PYBIN_SET" -eq 0 ] \
    && command -v hscc >/dev/null 2>&1
}

# Run one daemon CLI subcommand, capturing combined stdout+stderr so a failure
# can surface the real reason (never a silent /dev/null).
run_daemon_cmd() {
  local sub="${1:-}"
  if using_console_script; then
    hscc "$sub" 2>&1
  else
    PYTHONPATH="${HSCC_PLUGIN_ROOT}${PYTHONPATH:+:$PYTHONPATH}" \
      "$HSCC_PYBIN" "$HSCC_CMD" "$sub" 2>&1
  fi
}

if $SKIP; then
  echo "daemon restart skipped (--skip-daemon)"
  exit 0
fi

# Only the path-A fallback needs HSCC_CMD to exist on disk; path B uses the
# console script and HSCC_CMD is just a dead default.
if ! using_console_script && [ ! -f "$HSCC_CMD" ]; then
  echo "daemon restart: hscc command not found ($HSCC_CMD) — new code may not be loaded; re-run hscc_daemon setup manually"
  exit 3
fi

PID0="$(daemon_pid)"

STOP_OUT="$(run_daemon_cmd stop)"
START_OUT="$(run_daemon_cmd start)"
# Real reason a restart may have failed, trimmed to a readable single line
# (first non-blank lines of captured stop/start output).
ERR="$(printf '%s\n' "$STOP_OUT" "$START_OUT" | sed '/^[[:space:]]*$/d' | head -n 4 | tr '\n' ' ' | sed 's/[[:space:]]*$//')"

# The freshly-forked daemon writes its pid file slightly asynchronously; poll a
# few times so we read the real new PID rather than racing it.
PID1=""
for _ in $(seq 1 "$POLL_ATTEMPTS"); do
  PID1="$(daemon_pid)"
  [ -n "$PID1" ] && break
  sleep "$POLL_INTERVAL"
done

if [ -z "$PID1" ]; then
  echo "daemon did not come back after restart — new code may not be loaded${ERR:+ (${ERR})}"
  exit 3
fi

if [ -n "$PID0" ] && [ "$PID0" != "$PID1" ]; then
  echo "daemon restarted (pid ${PID0} -> ${PID1})"
  exit 0
fi

if [ -z "$PID0" ]; then
  # was not running before (fresh install) — nothing to turn over; new code is
  # loaded and running, which is the whole point
  echo "daemon started (pid ${PID1})"
  exit 0
fi

echo "daemon PID unchanged after restart (${PID0}) — new code may not be loaded${ERR:+ (${ERR})}"
exit 2
