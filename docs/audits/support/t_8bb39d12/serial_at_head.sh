#!/bin/sh
# One full scripts/tests suite in the FOREGROUND at the shipping commit, trace
# on, with the [2-130] arm's markers asserted. This is the proof bar's serial
# run: the pair runs above it were taken at the fix commit, before this card's
# docs-index entries existed (the lint test rightly flagged that), so the green
# serial run is re-taken at the tree that ships.
set -u
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PY=${PY:-$HOME/.hermes/hermes-agent/venv/bin/python}
W=${1:?worktree}
TAGLABEL=${2:-serial-at-head}
cd "$W" || exit 1
: >"$HERE/proof/trace.$TAGLABEL.txt"
env -u HERMES_DELEGATED_CHILD_CONTEXT HERMES_DISABLE_LAZY_INSTALLS=1 \
    HSCC_LOAD_BURNERS_TRACE="$HERE/proof/trace.$TAGLABEL.txt" \
    "$PY" -m pytest -q -rs scripts/tests -p no:cacheprovider -m "not loadstress" \
    >"$HERE/proof/$TAGLABEL.log" 2>&1
rc=$?
{
    printf '%s: rc=%s | %s\n' "$TAGLABEL" "$rc" "$(tail -1 "$HERE/proof/$TAGLABEL.log" | tr -s ' ')"
    printf '%s: arm-failure-mentions=%s int-trap=%s cleanup-exit130=%s sigint-reset=%s\n' \
        "$TAGLABEL" \
        "$(grep -c 'signal_to_the_harness_takes_every_burner_with_it\[2-130\]' "$HERE/proof/$TAGLABEL.log")" \
        "$(grep -c ' int-trap' "$HERE/proof/trace.$TAGLABEL.txt")" \
        "$(grep -c 'cleanup-exit 130' "$HERE/proof/trace.$TAGLABEL.txt")" \
        "$(grep -c ' sigint-reset' "$HERE/proof/trace.$TAGLABEL.txt")"
    printf '%s: other-failures=%s\n' "$TAGLABEL" \
        "$(grep -cE '^FAILED|____ test_' "$HERE/proof/$TAGLABEL.log")"
    printf '%s: survivors=[%s]\n' "$TAGLABEL" "$(pgrep -f 'hscc-load-test-' | tr '\n' ' ')"
} >>"$HERE/proof/results.txt"
for p in $(pgrep -f 'hscc-load-test-' 2>/dev/null); do kill -KILL "$p" 2>/dev/null; done
echo "SERIAL AT HEAD DONE" >>"$HERE/proof/results.txt"
