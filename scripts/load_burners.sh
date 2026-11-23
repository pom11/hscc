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
#   * GROUP. Burners (and the command under test) are started under `set -m`,
#     so each background job is its own process-group leader. `$!` is then a
#     PGID, and `kill -- -PGID` reaches the whole group even after the leader
#     has exited. `kill $PID` is exactly the shape that failed on 2026-11-23.
#   * TRAP. EXIT, INT, TERM and HUP all route through cleanup(), which TERMs
#     every group (watchdog, command, burners), waits a short bounded grace,
#     then KILLs every group. It is idempotent, so a signal landing during
#     cleanup cannot kill only half the burners.
#   * HARD TIMEOUT. A watchdog subshell holds a `sleep <timeout>`, then writes
#     a marker and TERMs this script. That is why the command runs in the
#     BACKGROUND and is awaited with `wait`: a POSIX shell defers a trap while
#     it waits for a FOREGROUND child (measured here: TERM was held for the
#     full 30 s of a foreground `sleep`), so a hung command would otherwise
#     run past the timeout and keep its burners. `wait` is interruptible, so
#     the trap fires on time and cleanup runs.
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

cleanup() {
    status=$?
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

    if [ -n "$WATCHDOG_PID" ]; then
        kill -TERM -- "-$WATCHDOG_PID" 2>/dev/null ||
            kill -TERM "$WATCHDOG_PID" 2>/dev/null || true
    fi
    if [ -n "$COMMAND_PID" ]; then
        kill -TERM -- "-$COMMAND_PID" 2>/dev/null ||
            kill -TERM "$COMMAND_PID" 2>/dev/null || true
    fi
    for pid in $BURNER_PIDS; do
        kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    done

    # Bounded grace, then the guarantee. TERM is what lets a well-behaved
    # burner/test clean up; KILL is what makes "no survivor" true regardless.
    sleep 0.2
    if [ -n "$COMMAND_PID" ]; then
        kill -KILL -- "-$COMMAND_PID" 2>/dev/null ||
            kill -KILL "$COMMAND_PID" 2>/dev/null || true
    fi
    for pid in $BURNER_PIDS; do
        kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
    done

    # Reap them here, with stderr quiet: otherwise bash announces every killed
    # background job on stderr, which reads like the harness went wrong when it
    # is exactly the guarantee working.
    for pid in $COMMAND_PID $WATCHDOG_PID $BURNER_PIDS; do
        [ -n "$pid" ] && wait "$pid" 2>/dev/null
    done

    [ -e "$TIMEOUT_MARK" ] && status=124
    rm -f "$TIMEOUT_MARK"
    exit "$status"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

# ── own the groups ──────────────────────────────────────────────────────────
# `set -m` in a non-interactive shell still gives each background job its own
# PGID (measured on this host): $! is the PGID, not merely a PID.
set -m
i=0
while [ "$i" -lt "$BURNERS" ]; do
    sh -c 'while :; do :; done' "$TAG" &
    BURNER_PIDS="$BURNER_PIDS $!"
    i=$((i + 1))
done
( sleep "$LIMIT"; : > "$TIMEOUT_MARK"; kill -TERM "$$" ) &
WATCHDOG_PID=$!

# The command runs in the background so `wait` (not a blocking foreground
# exec) is what the trap interrupts. Its group is owned and killed like the
# burners': a timed-out command must not outlive the harness either.
"$@" &
COMMAND_PID=$!
set +m

wait "$COMMAND_PID"
status=$?
exit "$status"
