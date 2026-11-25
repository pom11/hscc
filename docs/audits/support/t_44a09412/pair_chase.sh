#!/bin/sh
# pair_chase.sh — the t_f8e6fe38 pair shape (two full scripts/tests suites, two
# worktrees, same commit) with the t_44a09412 signal-state probe attached to the
# stalled harness (t_44a09412).
#
# The pair shape is the measured repro: it failed the SIGINT arm 4/4 suite-runs
# (t_5f9fab03 pair runs 0/1) and 2/2 (run 2). Each suite traces its harnesses
# into its own HSCC_LOAD_BURNERS_TRACE file; one watcher per suite polls for a
# load_burners.sh harness older than MINAGE whose trace stops at in-wait, and
# probes it before the test's own terminate() ends the window (~10 s).
#
# usage: pair_chase.sh <worktreeA> <worktreeB> <out-dir> [python]
set -u
A=${1:?worktreeA}
B=${2:?worktreeB}
OUT=${3:?outdir}
PY=${4:-$HOME/.hermes/hermes-agent/venv/bin/python}
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
MINAGE=${MINAGE:-4}
mkdir -p "$OUT" || exit 1

[ -x "$HERE/sigprobe" ] || cc -O1 -o "$HERE/sigprobe" "$HERE/sigprobe.c" || exit 1
"$PY" -m pytest --version >>"$OUT/launch.log" 2>&1 || { echo "no pytest in $PY"; exit 1; }

suite() {  # $1=worktree $2=trace
    cd "$1" || exit 1
    env -u HERMES_DELEGATED_CHILD_CONTEXT HERMES_DISABLE_LAZY_INSTALLS=1 \
        HSCC_LOAD_BURNERS_TRACE="$2" \
        "$PY" -m pytest -q -rs scripts/tests -p no:cacheprovider -m "not loadstress"
}

: >"$OUT/traceA.txt"; : >"$OUT/traceB.txt"
echo "launch $(date)" >"$OUT/launch.log"
( suite "$A" "$OUT/traceA.txt" >"$OUT/suiteA.log" 2>&1 ) &
pa=$!
( suite "$B" "$OUT/traceB.txt" >"$OUT/suiteB.log" 2>&1 ) &
pb=$!
echo "suiteA pid=$pa suiteB pid=$pb" >>"$OUT/launch.log"

sh "$HERE/probe_stalled.sh" "$OUT/traceA.txt" "$OUT/probeA.log" 1 "$MINAGE" \
    >>"$OUT/launch.log" 2>&1 &
wa=$!
sh "$HERE/probe_stalled.sh" "$OUT/traceB.txt" "$OUT/probeB.log" 1 "$MINAGE" \
    >>"$OUT/launch.log" 2>&1 &
wb=$!

wait "$pa" "$pb"
echo "suites done $(date)" >>"$OUT/launch.log"
# The probe must not outlive the suites, and must not leave burners behind.
kill -TERM "$wa" "$wb" 2>/dev/null
for p in $(pgrep -f "load_burners.sh" 2>/dev/null); do kill -TERM "$p" 2>/dev/null; done
sleep 1
for p in $(pgrep -f "hscc-load-test-" 2>/dev/null); do kill -KILL "$p" 2>/dev/null; done
echo "post-run survivors: $(pgrep -f 'hscc-load-test-' | tr '\n' ' ')[none]" >>"$OUT/launch.log"
grep -h -E "failed|passed" "$OUT/suiteA.log" "$OUT/suiteB.log" | tail -4 >>"$OUT/launch.log"
