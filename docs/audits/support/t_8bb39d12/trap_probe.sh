#!/bin/sh
# Can a shell tell, without forking, that its own `trap … INT` did not install?
#
# This decides how load_burners.sh detects the inherited ignore. `trap -p INT`
# is the only candidate that costs no process, and it is not POSIX-specified,
# so it has to be measured — including against the case that matters: SIGINT
# inherited as SIG_IGN, where the shell must refuse the trap.
set -u
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PY=${PY:-$HOME/.hermes/hermes-agent/venv/bin/python}
SH=${SH:-/bin/sh}
PROG="$HERE/.trap_probe.inner"
cat >"$PROG" <<'EOF'
echo "  trap -p BEFORE : [$(trap -p INT 2>&1)]"
trap 'echo INT-RAN; exit 130' INT
echo "  trap -p AFTER  : [$(trap -p INT 2>&1)]"
echo "  kernel says    : $(${SP:-$HERE/sigprobe} $$ 2>/dev/null | grep -E 'ignored|caught' | tr -s ' ' | tr '\n' '|')"
echo -n "  real SIGINT to myself: "; kill -INT $$ 2>/dev/null; sleep 0.4; echo "(survived)"
EOF

echo "--- $SH ($($SH --version 2>&1 | head -1)) ---"
for mode in dfl ign; do
    if [ "$mode" = dfl ]; then
        echo "[inherited SIGINT = SIG_DFL]"
    else
        echo "[inherited SIGINT = SIG_IGN (what cmd & hands down)]"
    fi
    "$PY" "$HERE/setdisp.py" "$mode" int -- "$SH" "$PROG" 2>&1 | sed 's/^/  /'
done
rm -f "$PROG"
