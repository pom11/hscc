#!/bin/sh
# Walk the launch chain that produces a stalled harness and read SIGINT's
# disposition at each link, from outside (sigprobe). Chain shape is the one
# scripts/tests runs under: worker shell -> sh <script> -> python -> Popen(
# start_new_session=True) -> /bin/sh load_burners.sh.
#
# The first link that shows `ignored INT=1` owns the bug: everything downstream
# inherits an ignore that bash 3.2 is not allowed to turn into a trap.
set -u
REPO=${1:?repo}
SP=${2:?sigprobe}
OUT=${3:?outfile}
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PY=${PY:-$HOME/.hermes/hermes-agent/venv/bin/python}

"$PY" "$HERE/chain_attr.py" "$REPO/scripts/load_burners.sh" "$OUT.pids" &
bg=$!
i=0
while [ ! -s "$OUT.pids" ] && [ "$i" -lt 100 ]; do sleep 0.1; i=$((i + 1)); done
. "$OUT.pids" 2>/dev/null || { sed 's/^/sh /' "$OUT.pids"; python_pid=$python_pid; }
echo "chain: launcher_sh=$launcher_pid python=$python_pid harness=$harness_pid"
for label in launcher_sh python harness; do
    case "$label" in
        launcher_sh) key=launcher_pid ;;
        python) key=python_pid ;;
        harness) key=harness_pid ;;
    esac
    pid=$(sed -n "s/^$key=//p" "$OUT.pids")
    echo "== $label pid=$pid =="
    "$SP" "$pid" 2>&1 | grep -E "^pid=|ignored|caught"
done
kill -TERM "$bg" 2>/dev/null
for p in $(pgrep -f "hscc-load-attr" 2>/dev/null); do kill -TERM "$p" 2>/dev/null; done
sleep 0.5
echo "survivors: $(pgrep -f 'hscc-load-attr' | tr '\n' ' ')[none]"
