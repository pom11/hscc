#!/bin/bash
# alone_sweep.sh — enumerate every test file in a leg run ALONE and diff it
# against the same leg run as one process (t_572b4856).
#
# Why this exists: a leg that prints ALL GREEN in one pytest process can still
# contain a file that FAILS, or skips for a different reason, when it is run by
# itself. The dual-interpreter stamp's promise — "a FAILED in a sibling leg is a
# regression" — is only worth what that is true for. Full write-up:
# docs/audits/daemon-alone-run-order-dependence_t_572b4856.md
#
# Usage:  alone_sweep.sh <python> <leg-dir> [out-dir]
# e.g.    alone_sweep.sh ~/miniconda3/envs/p313/bin/python hscc_daemon /tmp/sweep-p313
#
# The in-leg reference runs THROUGH scripts/run_tests.sh (so it gets the runner's
# own environment, including the SUITE_ENV_UNSET scrub) with --junitxml added —
# extra argv is forwarded to pytest by design. The alone side mirrors that argv
# and mirrors the scrub explicitly, so the only variable is the file set.
set -u
PY="${1:?usage: alone_sweep.sh <python> <leg-dir> [out-dir]}"
LEG="${2:?usage: alone_sweep.sh <python> <leg-dir> [out-dir]}"
OUT="${3:-/tmp/alone-sweep-$(basename "$LEG")-$$}"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
mkdir -p "$OUT/xml" "$OUT/logs" "$OUT/rc" || exit 99
cd "$REPO" || exit 99

echo "== in-leg reference (one process, via the canonical runner) -> $OUT/inleg.log"
HSCC_TEST_PY="$PY" HSCC_TEST_LEGS="$LEG" \
  bash scripts/run_tests.sh --junitxml="$OUT/inleg.xml" > "$OUT/inleg.log" 2>&1
LEG_RC=$?
echo "   run_tests rc=$LEG_RC  $(grep -E '^[0-9]+ (passed|failed)' "$OUT/inleg.log" | tail -1)"

echo "== alone sweep (one process per file)"
for f in "$LEG"/tests/test_*.py; do
  b=$(basename "$f" .py)
  [ -f "$OUT/rc/$b.rc" ] && continue          # resumable
  # -u list mirrors scripts/run_tests.sh SUITE_ENV_UNSET.
  env -u HERMES_DELEGATED_CHILD_CONTEXT -u HERMES_KANBAN_DB \
      -u HERMES_KANBAN_BOARD -u HERMES_KANBAN_WORKSPACES_ROOT \
      -u HERMES_KANBAN_ATTACHMENTS_ROOT \
    "$PY" -m pytest -q -rs "$f" -p no:cacheprovider \
      --junitxml="$OUT/xml/$b.xml" > "$OUT/logs/$b.log" 2>&1
  echo $? > "$OUT/rc/$b.rc"
done

echo "== diff (per nodeid from junit, plus per skip-reason from -rs)"
"$PY" "$HERE/diff_alone_vs_leg.py" "$OUT"
DIFF_RC=$?
echo
if [ "$LEG_RC" -ne 0 ]; then
  echo "RESULT: the LEG ITSELF is red — fix that before reading the diff."
  exit 1
fi
[ "$DIFF_RC" -ne 0 ] && echo "RESULT: alone != in-leg for at least one file (see above)."
[ "$DIFF_RC" -eq 0 ] && echo "RESULT: alone == in-leg for every file swept."
exit "$DIFF_RC"
