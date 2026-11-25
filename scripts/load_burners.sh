#!/bin/sh
# Run a command under disposable CPU burners, and guarantee the burners die
# with this script (t_71ed3b1c).
#
# Usage:
#   scripts/load_burners.sh <burners> <timeout-seconds> -- <command> [args...]
#
# Why this exists. On 2026-11-23 an ad-hoc load test spawned twelve
# `while :; do :; done` burners and its `kill $BURNERS` never reached them: the
# burners were children of a shell that had already gone, kept running as
# orphans at ~90% CPU each, and held the box at load 24 for 3h40m. A PID is not
# enough to own a burner, and a cleanup line after the command is not enough to
# own it either — it never runs when the test hangs, is interrupted, or exits
# through a path its author did not think of.
#
# The ownership shape, all three legs of it:
#
#   * GROUP. Burners (and the command under test) are each started through an
#     exec wrapper that makes the child its own process-group leader BEFORE it
#     runs: `setsid` where available, otherwise a python3 `os.setsid()` /
#     `os.setpgid(0, 0)` shim. The wrapper execs, so `$!` is the leader's PID
#     and also its PGID, and `kill -TERM -PGID` reaches the whole group even
#     after the leader has exited. `kill $PID` is exactly the shape that failed
#     on 2026-11-23. `set -m` is deliberately NOT used: in a non-interactive
#     shell on Linux it can leave background jobs in the harness's own PGID,
#     which is how this harness passed on macOS and failed on CI.
#   * TRAP. EXIT, INT, TERM and HUP all route through cleanup(), which TERMs
#     every group (watchdog, command, burners), waits a short bounded grace,
#     then KILLs every group. It is idempotent, so a signal landing during
#     cleanup cannot kill only half the burners.
#   * HARD TIMEOUT. A watchdog subshell holds a `sleep <timeout>`, then writes
#     a marker and TERMs this script. That is why the command runs in the
#     BACKGROUND and is awaited in bounded slices: a POSIX shell defers a trap
#     while it waits for a FOREGROUND child (measured here: TERM was held for
#     the full 30 s of a foreground `sleep`), so a hung command would otherwise
#     run past the timeout and keep its burners. `wait` is interruptible in
#     principle, but bash 3.2 (macOS /bin/sh) has a MEASURED race (t_5f9fab03,
#     docs/audits/load-burners-sigint-trace-stage_t_5f9fab03.md): a SIGINT
#     delivered in a narrow window can be queued and NEVER dispatched while
#     blocked in `wait "$COMMAND_PID"` — the harness slept 10 s with its
#     burners running and then died to the test's SIGTERM without running THAT
#     trap either. The command-boundary deferral (held, then RUN) is the one
#     POSIX shells are relied on to flush, so the wait loop below makes the
#     worst-case trap latency one poll (0.1 s) by construction instead of
#     trusting the racy wakeup.
#
# Burner argv carries a unique tag (`$0` of each `sh -c`, overridable with
# HSCC_LOAD_BURNER_TAG) so a guard can pgrep for survivors without matching
# unrelated processes. The tag is an argument, never an env-only marker:
# `pgrep -f` sees argv, not the environment.
#
# Exit status: the command's own status; 124 when the hard timeout fired;
# 129/130/143 for HUP/INT/TERM. 64 is a usage error, 70 could not start.
#
# This script is a hammer, not a benchmark: it exists to reproduce "the suite
# under CPU pressure", so keep burner counts and timeouts tiny in tests and CI.
set -u

usage() {
    echo "usage: $0 <burners> <timeout-seconds> -- <command> [args...]" >&2
    exit 64
}

[ "$#" -ge 3 ] || usage
BURNERS=$1
LIMIT=$2
shift 2
[ "${1:-}" = "--" ] && shift
[ "$#" -gt 0 ] || usage

case $BURNERS in
    ''|*[!0-9]*) usage ;;
esac
# Positive integer or decimal seconds. Reject an empty/zero/non-numeric budget:
# a load harness whose timeout can be 0 is a harness that can skip its trap.
case $LIMIT in
    ''|0|0.0|0.00|.*|*.|*[!0-9.]*|*.*.*) usage ;;
esac

TAG=${HSCC_LOAD_BURNER_TAG:-hscc-load-burner-$$}
TIMEOUT_MARK=$(mktemp "${TMPDIR:-/tmp}/hscc-load-burners.XXXXXX") || exit 70
rm -f "$TIMEOUT_MARK"

BURNER_PIDS=""
WATCHDOG_PID=""
COMMAND_PID=""
CLEANED=0

# Signal a whole process group, falling back to the leader alone if the
# platform's kill rejects the group form. The group is the contract; the
# fallback exists only so cleanup still does something on an odd host.
kill_group() {
    sig=$1
    pid=$2
    [ -n "$pid" ] || return 0
    kill "-$sig" "-$pid" 2>/dev/null || kill "-$sig" "$pid" 2>/dev/null || true
}

# ── opt-in stage trace (t_5f9fab03) ─────────────────────────────────────────
# HSCC_LOAD_BURNERS_TRACE=<path>: append one line per harness stage,
# "<pid> <tag> <stage> [detail]". Unset ⇒ lb_trace returns before any syscall:
# zero behaviour change for every shipped consumer (loadstress_leg.py,
# run_tests.sh, test_load_burners.py, test_loadstress_leg.py). It exists
# because the sibling-suite SIGINT stall
# (docs/audits/load-burners-signal-sibling-liveness_t_f8e6fe38.md) cannot be
# told apart from outside: "the INT trap never ran" and "a reap is blocking"
# look identical to pgrep and to the test — only the harness itself knows
# which stage it reached.
#
# The `2>/dev/null` comes BEFORE the append redirection on purpose: bash
# applies redirections left to right, so a broken trace path reports onto the
# already-redirected stderr and can never print what the harness contract
# forbids. Markers never touch stdout.
lb_trace() {
    [ -n "${HSCC_LOAD_BURNERS_TRACE:-}" ] || return 0
    printf '%s %s %s %s\n' "$$" "$TAG" "$1" "${2:-}" \
        2>/dev/null >>"$HSCC_LOAD_BURNERS_TRACE" || true
}

cleanup() {
    status=$?
    lb_trace cleanup-entry
    # A signal arriving during cleanup must not re-enter and kill half the
    # groups; the first pass owns all of them.
    [ "$CLEANED" -eq 1 ] && return
    CLEANED=1
    trap - EXIT INT TERM HUP
    # Cleanup's own noise is not the operator's problem: the command's stderr
    # has already passed through, and bash's "Terminated" job notices here read
    # like a failure when they are the guarantee working. Kill errors are
    # deliberately ignored below (ESRCH means the group already went).
    exec 2>/dev/null

    kill_group TERM "$WATCHDOG_PID"
    kill_group TERM "$COMMAND_PID"
    for pid in $BURNER_PIDS; do
        kill_group TERM "$pid"
    done
    lb_trace after-term-loop

    # Bounded grace, then the guarantee. TERM is what lets a well-behaved
    # burner/test clean up; KILL is what makes "no survivor" true regardless.
    sleep 0.2
    lb_trace after-sleep
    kill_group KILL "$WATCHDOG_PID"
    kill_group KILL "$COMMAND_PID"
    for pid in $BURNER_PIDS; do
        kill_group KILL "$pid"
    done
    lb_trace after-kill-loop

    # Reap them here, with stderr quiet: otherwise bash announces every killed
    # background job on stderr, which reads like the harness went wrong when it
    # is exactly the guarantee working.
    for pid in $COMMAND_PID $WATCHDOG_PID $BURNER_PIDS; do
        if [ -n "$pid" ]; then
            lb_trace reap-wait "$pid"
            wait "$pid" 2>/dev/null
            lb_trace reap-ok "$pid"
        fi
    done

    [ -e "$TIMEOUT_MARK" ] && status=124
    rm -f "$TIMEOUT_MARK"
    lb_trace cleanup-exit "$status"
    exit "$status"
}

trap cleanup EXIT
trap 'lb_trace int-trap; exit 130' INT
trap 'lb_trace term-trap; exit 143' TERM
trap 'lb_trace hup-trap; exit 129' HUP
lb_trace start

# ── own the groups ──────────────────────────────────────────────────────────
# NOT `set -m`: job control is unreliable in a non-interactive shell on Linux
# (the child can inherit the harness's PGID), which is why this passed on
# macOS and failed on CI. Instead each child is launched through an exec
# wrapper that makes it a process-group leader before exec'ing the real
# program. The wrapper execs rather than forks, so `$!` is both the PID and
# the PGID, and `kill -TERM -$!` owns the whole group. Background jobs run
# without job control, so the child is not already a group leader and
# setsid(1) therefore execs in place instead of forking.
if command -v setsid >/dev/null 2>&1; then
    new_group() { exec setsid "$@"; }
elif command -v python3 >/dev/null 2>&1; then
    new_group() {
        exec python3 -c 'import os, sys
try:
    os.setsid()
except OSError:
    try:
        os.setpgid(0, 0)
    except OSError:
        pass
os.execvp(sys.argv[1], sys.argv[1:])' "$@"
    }
else
    echo "$0: need setsid or python3 to give each child its own process group" >&2
    exit 70
fi

i=0
while [ "$i" -lt "$BURNERS" ]; do
    new_group sh -c 'while :; do :; done' "$TAG" &
    BURNER_PIDS="$BURNER_PIDS $!"
    i=$((i + 1))
done
new_group sh -c 'sleep "$1"; : > "$2"; kill -TERM "$3"' \
    sh "$LIMIT" "$TIMEOUT_MARK" "$$" &
WATCHDOG_PID=$!

# The command runs in the background so no signal must interrupt a blocking
# foreground exec of it. Its group is owned and killed like the burners': a
# timed-out command must not outlive the harness either.
new_group "$@" &
COMMAND_PID=$!

lb_trace in-wait "$COMMAND_PID"
# Await the command in bounded foreground slices, never in one unbounded
# `wait`: bash 3.2 measured (see header; t_5f9fab03) can queue a SIGINT in a
# narrow race and never dispatch its trap while blocked in `wait`. A trap
# deferred by a FOREGROUND command is the deferral POSIX shells reliably
# flush at the command boundary, so `sleep 0.1` bounds worst-case trap
# latency to one poll on any POSIX shell (bash 3.2 and dash both run this
# loop identically). The final `wait` only collects the exit status of the
# now-dead child.
while kill -0 "$COMMAND_PID" 2>/dev/null; do
    sleep 0.1
done
wait "$COMMAND_PID" 2>/dev/null
status=$?
exit "$status"
