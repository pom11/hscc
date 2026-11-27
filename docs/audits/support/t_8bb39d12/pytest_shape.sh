#!/bin/sh
# The real SIGINT-arm test file, run under pytest in four launcher shapes, to
# separate "how the suite was launched" from "what the suite contains".
#
#   fg        pytest as a synchronous child of a non-interactive shell
#   bg        the same command as `cmd &` (what pair_chase.sh and the
#             pack mode of scripts/run_tests.sh:887-899 do)
#   fg_ign    foreground, but SIGINT/QUIT forced to SIG_IGN first (no `&` at
#             all: proves the async shape is not needed, only the disposition)
#   bg_reset  `cmd &`, but SIGINT/QUIT reset to SIG_DFL before exec (the fix
#             direction: proves the async shape is not sufficient either)
#
# usage: pytest_shape.sh <fg|bg|fg_ign|bg_reset>
set -u
SHAPE=${1:?fg|bg|fg_ign|bg_reset}
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$HERE" || exit 1
PY=${PY:-$HOME/.hermes/hermes-agent/venv/bin/python}
SP=./sigprobe
REPO=${REPO:-$HERE/../hscc}
TEST=${TEST:-scripts/tests/test_load_burners.py}
PIDF="$HERE/pytest.$SHAPE.pid"
TRACE="$HERE/trace.$SHAPE.txt"
OUT="$HERE/suite.$SHAPE.log"
: >"$PIDF"; : >"$TRACE"

watch() {
    i=0; pid=""
    while [ "$i" -lt 100 ]; do
        pid=$(cat "$PIDF" 2>/dev/null); [ -n "$pid" ] && break
        sleep 0.1; i=$((i + 1))
    done
    sleep 4
    [ -n "$pid" ] || return 0
    kill -0 "$pid" 2>/dev/null || return 0
    echo "  pytest pid=$pid while the suite runs:"
    "$SP" "$pid" 2>&1 | sed 's/^/    /'
}

case $SHAPE in
    fg|bg)     set -- ;;
    fg_ign)    set -- "$PY" "$HERE/setdisp.py" ign int quit -- ;;
    bg_reset)  set -- "$PY" "$HERE/setdisp.py" dfl int quit -- ;;
    *) echo "bad shape"; exit 64 ;;
esac
SYNC=sync
case $SHAPE in bg|bg_reset) SYNC=async ;; esac

run_pytest() {
    cd "$REPO" || exit 1
    HSCC_LOAD_BURNERS_TRACE="$TRACE"
    export HSCC_LOAD_BURNERS_TRACE
    exec "$PY" "$HERE/pidexec.py" "$PIDF" "$@" \
        "$PY" -m pytest -q -rs "$TEST" -p no:cacheprovider
}

echo "== shape=$SHAPE launch=$SYNC test=$TEST =="
if [ "$SYNC" = async ]; then
    watch & wp=$!
    ( run_pytest "$@" ) >"$OUT" 2>&1 &
    hp=$!; wait "$hp"; rc=$?; wait "$wp" 2>/dev/null
else
    ( sleep 0.3; watch ) & wp=$!
    ( run_pytest "$@" ) >"$OUT" 2>&1
    rc=$?; wait "$wp" 2>/dev/null
fi

echo "  pytest rc=$rc"
grep -E "^(FAILED|ERROR)" "$OUT" | sed 's/^/    /'
tail -1 "$OUT" | sed 's/^/    /'
echo "  int-trap markers in the harness trace: $(grep -c 'int-trap' "$TRACE" 2>/dev/null || echo 0)"
echo "  cleanup-exit markers: $(grep -c 'cleanup-exit' "$TRACE" 2>/dev/null || echo 0)"
for p in $(pgrep -f 'hscc-load-test-' 2>/dev/null); do kill -KILL "$p" 2>/dev/null; done
echo
