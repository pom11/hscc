#!/bin/sh
# control_calibrate.sh — prove probe_stalled.sh can tell "blocked in the kernel
# mask" from "delivered and lost inside bash", BEFORE trusting its answer about
# a real stalled harness (t_44a09412).
#
# Three states, two of them manufactured on demand:
#
#   A  sigprobe --blockexec nests TSTP(20)+QUIT(3)+INT(2) into the harness's
#      mask before exec (/bin/sh inherits it) — the harness then sits in the SAME
#      `wait "$COMMAND_PID"` the real stall sits in, but its mask provably holds
#      the whole bash block_ints set. Expected probe reading: TSTP not delivered,
#      second INT not delivered, QUIT not delivered.
#   A' only INT blocked: expected TSTP DELIVERED (stops), INT not delivered,
#      QUIT DELIVERED. Shows the probe is signal-specific, not "process deaf".
#   B  a real harness in the same wait whose mask is clean and which has NOT
#      been signalled: expected TSTP delivered, QUIT delivered.
#
# If A and B do not read differently, probe_stalled.sh is blind and the verdict
# from a live stall would be worthless.
#
# usage: control_calibrate.sh <repo-with-load_burners.sh> <out-dir>
set -u
REPO=${1:?repo}
OUT=${2:?outdir}
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
mkdir -p "$OUT"
HARNESS="$REPO/scripts/load_burners.sh"
SIGPROBE="$HERE/sigprobe"
[ -x "$SIGPROBE" ] || cc -O1 -o "$SIGPROBE" "$HERE/sigprobe.c" || exit 1

sweep_kill() {                      # never leave a burner behind (t_71ed3b1c)
    for p in $(pgrep -f "hscc-load-calib" 2>/dev/null); do
        kill -TERM "$p" 2>/dev/null
    done
    sleep 0.4
    for p in $(pgrep -f "hscc-load-calib" 2>/dev/null); do
        kill -KILL "$p" 2>/dev/null
    done
}
trap sweep_kill EXIT INT TERM

# One harness, traced, parked in `wait`; $1 = tag, $2.. = the launcher.
park_harness() {
    tag=$1; shift
    HSCC_LOAD_BURNER_TAG="$tag" \
    HSCC_LOAD_BURNERS_TRACE="$OUT/trace.txt" \
        "$@" /bin/sh "$HARNESS" 1 60 -- sleep 60 >/dev/null 2>&1 &
    p=$!
    # Give it a moment to fork its burner + watchdog + command and reach wait.
    i=0
    while [ "$i" -lt 50 ]; do
        grep -q "^$p .* in-wait" "$OUT/trace.txt" 2>/dev/null && break
        i=$((i + 1)); sleep 0.1
    done
    echo "$p"
}

probe_one() {   # $1=pid $2=label  — the same reads probe_stalled.sh makes
    log="$OUT/$2.txt"
    {
        echo "== $2 pid=$1 sysctl =="
        "$SIGPROBE" --sample "$1" 300 2
        echo "== $2 TSTP =="
        kill -TSTP "$1" 2>/dev/null
        sleep 0.6
        st=$(ps -o stat= -p "$1" 2>/dev/null | tr -d ' ')
        case "$st" in T*) echo "TSTP DELIVERED stat=$st" ;; *) echo "TSTP NOT DELIVERED stat=$st" ;; esac
        kill -CONT "$1" 2>/dev/null; sleep 0.3
        echo "== $2 second INT =="
        kill -INT "$1" 2>/dev/null; sleep 1.2
        if kill -0 "$1" 2>/dev/null; then echo "INT NOT DELIVERED"; else echo "INT DELIVERED"; fi
        echo "== $2 QUIT =="
        if kill -0 "$1" 2>/dev/null; then
            kill -QUIT "$1" 2>/dev/null; sleep 1.2
            if kill -0 "$1" 2>/dev/null; then echo "QUIT NOT DELIVERED"; else echo "QUIT DELIVERED"; fi
        fi
    } >"$log" 2>&1
    grep -E "TSTP|INT|QUIT" "$log" | sed "s/^/[$2] /"
    kill -TERM "$1" 2>/dev/null
}

: >"$OUT/trace.txt"

echo "### A: mask provably blocks TSTP+QUIT+INT (dispositions reset to DFL)"
pa=$(park_harness hscc-load-calib-A \
    "$SIGPROBE" --prep u2,u3,u20,b2,b3,b20 --)
echo "A harness pid=$pa"; grep "^$pa " "$OUT/trace.txt" | tail -2
probe_one "$pa" A-blocked-set

echo "### A': mask blocks ONLY INT (dispositions reset to DFL)"
pb=$(park_harness hscc-load-calib-Ap "$SIGPROBE" --prep u2,u3,u20,b2 --)
echo "A' harness pid=$pb"; grep "^$pb " "$OUT/trace.txt" | tail -2
probe_one "$pb" Ap-blocked-int-only

echo "### B: clean mask + default dispositions, same wait, unsignalled"
pc=$(park_harness hscc-load-calib-B "$SIGPROBE" --prep u2,u3,u20 --)
echo "B harness pid=$pc"; grep "^$pc " "$OUT/trace.txt" | tail -2
probe_one "$pc" B-clean-mask

echo "### expected: A = three NOT DELIVERED; A' = TSTP delivered, INT not, QUIT"
echo "### delivered; B = TSTP delivered + QUIT delivered. A vs B must differ."
sweep_kill
echo "### burners after sweep: $(pgrep -f hscc-load-calib | tr '\n' ' ')[none]"
