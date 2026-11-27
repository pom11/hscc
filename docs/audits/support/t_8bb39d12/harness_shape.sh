#!/bin/sh
# Deterministic per-shape readout of the REAL harness: for a given launcher
# shape, report (1) the harness's own SIGINT disposition, (2) whether it
# responds to a SIGINT at all, (3) its exit status.
#
# No pytest, no timing race: the harness sits in `wait` on `sleep 60` for a
# minute, so the probe always lands inside the window the test would have to
# win. The only variable is how the harness was launched.
#
# usage: harness_shape.sh <fg|bg|fg_ign|bg_reset>
set -u
SHAPE=${1:?fg|bg|fg_ign|bg_reset}
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$HERE" || exit 1
PY=${PY:-$HOME/.hermes/hermes-agent/venv/bin/python}
SP=./sigprobe
REPO=${REPO:-$HERE/../hscc}
HARNESS="$REPO/scripts/load_burners.sh"
PIDF="$HERE/harness.$SHAPE.pid"
TAG=hscc-shape-$SHAPE
TRACE="$HERE/harness.$SHAPE.trace"
export HSCC_LOAD_BURNERS_TRACE="$TRACE"
: >"$PIDF"; : >"$TRACE"

watch_and_signal() {
    i=0
    pid=""
    while [ "$i" -lt 100 ]; do
        pid=$(cat "$PIDF" 2>/dev/null)
        [ -n "$pid" ] && break
        sleep 0.1; i=$((i + 1))
    done
    sleep 1.5
    echo "  harness pid=$pid $(ps -o comm= -p "$pid" 2>/dev/null)"
    "$SP" "$pid" 2>&1 | sed 's/^/    /'
    kill -INT "$pid" 2>/dev/null
    sleep 2
    if kill -0 "$pid" 2>/dev/null; then
        echo "  SIGINT: NOT DELIVERED (alive 2s after INT) -> only KILL can take it"
        kill -KILL "$pid" 2>/dev/null
    else
        echo "  SIGINT: delivered"
    fi
}

# The launcher prefix, as positional args (no arrays: this file must stay
# readable by the same POSIX shells the harness claims to support).
case $SHAPE in
    fg|bg)     set -- ;;
    fg_ign)    set -- "$PY" "$HERE/setdisp.py" ign int quit -- ;;
    bg_reset)  set -- "$PY" "$HERE/setdisp.py" dfl int quit -- ;;
    *) echo "bad shape"; exit 64 ;;
esac
LAUNCH="$@"
SYNC=sync
case $SHAPE in bg|bg_reset) SYNC=async ;; esac
echo "shape=$SHAPE launch=$SYNC  [$LAUNCH]"

if [ "$SYNC" = async ]; then
    watch_and_signal &
    wp=$!
    ( exec "$PY" "$HERE/pidexec.py" "$PIDF" "$@" /bin/sh "$HARNESS" 1 60 -- sleep 60 ) &
    hp=$!
    wait "$hp"; rc=$?
    wait "$wp" 2>/dev/null
else
    # The probe is the async one; the harness stays a synchronous child, which
    # is the whole difference the POSIX async-command rule turns on.
    ( sleep 0.3; watch_and_signal ) &
    wp=$!
    ( exec "$PY" "$HERE/pidexec.py" "$PIDF" "$@" /bin/sh "$HARNESS" 1 60 -- sleep 60 )
    rc=$?
    wait "$wp" 2>/dev/null
fi
echo "  exit status: $rc  (130 = the INT trap ran; 137 = only the probe's KILL took it)"
for p in $(pgrep -f "$TAG" 2>/dev/null); do kill -KILL "$p" 2>/dev/null; done
echo
