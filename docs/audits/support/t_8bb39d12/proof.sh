#!/bin/sh
# proof.sh — the t_5f9fab03 proof bar, run against the FIXED harness.
#
#   pair runs 0..2 : two full scripts/tests suites, two worktrees, same commit,
#                    each suite tracing its harnesses; the [2-130] arm must pass
#                    on EVERY run AND its harness must show int-trap +
#                    cleanup-exit 130 (non-vacuity: "the case can pass by never
#                    observing the signal", t_f8e6fe38 LEFT-4). Under this
#                    launcher the harness is born with SIGINT ignored, so
#                    `sigint-reset` must also appear — the fix path is the one
#                    that ran.
#   serial         : one full suite, foreground, same assertions minus the reset.
#   alone          : the two shared-consumer files with the trace UNSET.
#
# Sequential on purpose: parallel suites have previously broken load-timing
# cases on this host.
set -u
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$HERE" || exit 1
PY=${PY:-$HOME/.hermes/hermes-agent/venv/bin/python}
A=${A:-$HERE/../A}
B=${B:-$HERE/../B}
OUT="$HERE/proof"
mkdir -p "$OUT" || exit 1
RES="$OUT/results.txt"
: >"$RES"

suite() {  # $1=worktree $2=tracefile $3=logfile
    cd "$1" || return 99
    env -u HERMES_DELEGATED_CHILD_CONTEXT HERMES_DISABLE_LAZY_INSTALLS=1 \
        HSCC_LOAD_BURNERS_TRACE="$2" \
        "$PY" -m pytest -q -rs scripts/tests -p no:cacheprovider -m "not loadstress" \
        >"$3" 2>&1
}

check_arm() {  # $1=run label $2=log $3=trace
    n_int=$(grep -c ' int-trap' "$3" 2>/dev/null || echo 0)
    n_exit=$(grep -c 'cleanup-exit 130' "$3" 2>/dev/null || echo 0)
    n_reset=$(grep -c ' sigint-reset' "$3" 2>/dev/null || echo 0)
    failed=$(grep -c 'signal_to_the_harness_takes_every_burner_with_it\[2-130\]' "$2" 2>/dev/null || echo 0)
    printf '%s: arm-failure-mentions=%s int-trap=%s cleanup-exit130=%s sigint-reset=%s | %s\n' \
        "$1" "$failed" "$n_int" "$n_exit" "$n_reset" "$(tail -1 "$2" | tr -s ' ')" >>"$RES"
    surv=$(pgrep -f 'hscc-load-test-' | tr '\n' ' ')
    printf '%s: survivors=[%s]\n' "$1" "$surv" >>"$RES"
    for p in $(pgrep -f 'hscc-load-test-' 2>/dev/null); do kill -KILL "$p" 2>/dev/null; done
}

r=0
while [ "$r" -lt 3 ]; do
    : >"$OUT/traceA.$r.txt"; : >"$OUT/traceB.$r.txt"
    suite "$A" "$OUT/traceA.$r.txt" "$OUT/suiteA.$r.log" &
    pa=$!
    suite "$B" "$OUT/traceB.$r.txt" "$OUT/suiteB.$r.log" &
    pb=$!
    wait "$pa"; wait "$pb"
    check_arm "pair$r/A" "$OUT/suiteA.$r.log" "$OUT/traceA.$r.txt"
    check_arm "pair$r/B" "$OUT/suiteB.$r.log" "$OUT/traceB.$r.txt"
    echo "pair$r done $(date +%H:%M:%S)" >>"$RES"
    r=$((r + 1))
done

: >"$OUT/traceS.txt"
suite "$A" "$OUT/traceS.txt" "$OUT/serial.log"
check_arm "serial" "$OUT/serial.log" "$OUT/traceS.txt"
echo "serial done $(date +%H:%M:%S)" >>"$RES"

# Shared consumers, trace UNSET: the shipped consumers must not need it.
cd "$A" || exit 1
env -u HERMES_DELEGATED_CHILD_CONTEXT HERMES_DISABLE_LAZY_INSTALLS=1 \
    "$PY" -m pytest -q -rs scripts/tests/test_load_burners.py \
    scripts/tests/test_loadstress_leg.py -p no:cacheprovider \
    >"$OUT/alone.log" 2>&1
rc=$?
printf 'alone (trace unset): rc=%s | %s\n' "$rc" "$(tail -1 "$OUT/alone.log" | tr -s ' ')" >>"$RES"
echo "ALL DONE $(date +%H:%M:%S)" >>"$RES"
