#!/usr/bin/env bash
# Leg heartbeat — scripts/run_tests.sh (t_ecb84764).
#
# WHAT IT PRINTS AND WHY NOTHING ELSE DOES
#
# A leg's consumer is not a human at a terminal: it is a dispatcher, a worker or
# the orch reading a REDIRECTED file to answer one question — is this leg slow,
# or is it stopped? Three workers reported the hscc-bootstrap leg as "a leg that
# cannot finish" before the real cause was found, because the log could not
# answer that question. pytest's `-q` output gives an outside reader:
#
#   * a `[ NN%]` marker that is a TEST-COUNT percent, not a time percent, printed
#     once per full 72-dot line — and this leg's wall time is wildly non-uniform
#     in collection order (measured at e1334386: at 24.6 % of tests ~41 % of the
#     700 s had gone; the 39-48 % band was ~48 % of the run). "43 %" therefore
#     cannot distinguish 10 s in from 300 s in.
#   * nothing at all carrying ELAPSED TIME. Not one number in the file says how
#     long the leg has been running.
#
# The two obvious fixes were measured and rejected:
#   PYTHONUNBUFFERED=1 — the dot band is NOT sitting unflushed. Controlled probe
#     (10 tests x 1 s sleep, stdout+stderr redirected, sampled once a second,
#     pytest 9.1.1, venv interpreter): one dot reaches the file per completed
#     test, live, and PYTHONUNBUFFERED=1 is byte-for-byte identical to not
#     setting it. It fixes a problem this suite does not have.
#   `-rA` — its per-test report lines land in the summary at the END of the run,
#     so they tell a mid-run reader nothing. `-v` does stream per test but still
#     carries no elapsed time, and rewrites the format every other reader greps.
# A pytest-side per-test hook cannot help either: pytest_runtest_logreport does
# not fire during the one test you are stuck in, which is exactly the case the
# reader is asking about. So the heartbeat lives OUTSIDE pytest, in the runner.
#
# It prints the two numbers that together separate slow from stopped:
#
#   elapsed — wall seconds since THIS leg started
#   cpu     — the pytest process's own CPU seconds (ps -o time)
#
#   elapsed rising, cpu rising  => slow but progressing: wait it out.
#   elapsed rising, cpu FLAT    => blocked on a wait/subprocess: go look now.
#
# The child count + first child answer the next question ("what is it waiting
# on"), with a warning attached, because that signal is what caused the
# misdiagnosis this card was filed from: a child names the SCRIPT it runs, not
# the test file that owns it. `bash <worktree>/hscc-bootstrap/restart_daemon.sh`
# is spawned by restart_daemon's own tests AND by bootstrap.sh:774 whenever a
# bootstrap_isolated / dry_run test runs the real installer. Read it as a hint,
# never as a verdict; --durations is what convicts.
#
# Cadence: HSCC_LEG_HEARTBEAT_SECONDS (default 30, integer seconds; 0 or a
# non-numeric value disables it). Output is one printf per line on the runner's
# stdout, so it lands in the same redirected file as the leg.
#
# Usage: leg_heartbeat.sh <leg-name> <runner_pid> <leg_start_epoch> [interval_s]
#
# The runner passes its OWN pid, not pytest's, because run_tests.sh must keep
# pytest in the FOREGROUND: backgrounding it would make a non-interactive shell
# install SIG_IGN on SIGINT, and Ctrl-C would stop reaching the suite. So this
# script discovers the leg's pytest as a child of the runner, excluding itself
# (it is a child too, and an older pid, so a naive `pgrep | head -1` would
# report the heartbeat watching itself).
set -u

LEG="${1:?usage: leg_heartbeat.sh <leg> <runner_pid> <leg_start_epoch> [interval_s]}"
RUNNER="${2:?missing runner pid}"
T0="${3:?missing leg start epoch}"
INTERVAL="${4:-30}"

case "$INTERVAL" in
  ''|*[!0-9]*) exit 0 ;;          # disabled / not an integer interval
esac
[ "$INTERVAL" -gt 0 ] || exit 0

# `kill -0` alone is NOT a liveness check: it succeeds on a ZOMBIE (the process
# is gone but not yet reaped), so a watcher whose runner exits without being
# waited would spin forever, printing a dead pid into the log AND holding the
# log's write end of the pipe open — which is exactly how a "leg that cannot
# finish" gets manufactured. Treat a zombie as gone.
alive() {
  kill -0 "$1" 2>/dev/null || return 1
  ps -o stat= -p "$1" 2>/dev/null | grep -q '^Z' && return 1
  return 0
}

while alive "$RUNNER"; do
  sleep "$INTERVAL"
  alive "$RUNNER" || break
  # self ($$) is a child of the runner too — never report the watcher.
  PY_PID="$(pgrep -P "$RUNNER" 2>/dev/null | grep -vx "$$" | head -1)"
  [ -n "$PY_PID" ] || continue
  CPU="$(ps -o time= -p "$PY_PID" 2>/dev/null | tr -d ' ')"
  KIDS="$(pgrep -P "$PY_PID" 2>/dev/null | wc -l | tr -d ' ')"
  FIRST_KID="none"
  FIRST="$(pgrep -P "$PY_PID" 2>/dev/null | head -1)"
  [ -n "$FIRST" ] && FIRST_KID="$(ps -o comm=,etime= -p "$FIRST" 2>/dev/null | tr -s ' ')"
  printf '[leg-heartbeat] leg=%s elapsed=%ss pytest_pid=%s cpu=%s children=%s first_child=%s\n' \
    "$LEG" "$(( $(date +%s) - T0 ))" "$PY_PID" "${CPU:-?}" "${KIDS:-0}" "$FIRST_KID"
done
