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
#   HSCC_CMD        path to the daemon CLI (default ~/.hermes/plugins/hscc_daemon/hscc.py)
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

HSCC_PYBIN="${HSCC_PYBIN:-$HOME/.hermes/hermes-agent/venv/bin/python}"
[ -x "$HSCC_PYBIN" ] || HSCC_PYBIN="python3"
HSCC_CMD="${HSCC_CMD:-$HOME/.hermes/plugins/hscc_daemon/hscc.py}"
HSCC_PID_FILE="${HSCC_PID_FILE:-$HOME/.hscc/daemon.pid}"
POLL_ATTEMPTS="${POLL_ATTEMPTS:-5}"
POLL_INTERVAL="${POLL_INTERVAL:-1}"

daemon_pid() {
  [ -f "$HSCC_PID_FILE" ] && cat "$HSCC_PID_FILE" 2>/dev/null || echo ""
}

if $SKIP; then
  echo "daemon restart skipped (--skip-daemon)"
  exit 0
fi

if [ ! -f "$HSCC_CMD" ]; then
  echo "daemon restart: hscc command not found ($HSCC_CMD) — new code may not be loaded; re-run hscc_daemon setup manually"
  exit 3
fi

PID0="$(daemon_pid)"

"$HSCC_PYBIN" "$HSCC_CMD" stop >/dev/null 2>&1
"$HSCC_PYBIN" "$HSCC_CMD" start >/dev/null 2>&1

# The freshly-forked daemon writes its pid file slightly asynchronously; poll a
# few times so we read the real new PID rather than racing it.
PID1=""
for _ in $(seq 1 "$POLL_ATTEMPTS"); do
  PID1="$(daemon_pid)"
  [ -n "$PID1" ] && break
  sleep "$POLL_INTERVAL"
done

if [ -z "$PID1" ]; then
  echo "daemon did not come back after restart — new code may not be loaded"
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

echo "daemon PID unchanged after restart (${PID0}) — new code may not be loaded"
exit 2
