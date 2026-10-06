#!/usr/bin/env bash
# Final verification for t_163fa09f (the p313 stop-no-op collection-order flake).
#
# WHY this wrapper exists instead of just calling scripts/run_tests.sh: the card's
# subject is a millisecond-scale thread race, so an unattributed "ALL GREEN" is
# worthless — a green log written before the isolation machinery was refactored
# describes code that no longer exists. Every log this script writes stamps the
# exact commit and worktree state it ran against, so a reviewer can re-check the
# claim by execution rather than by timestamp arithmetic.
#
# Runs are SEQUENTIAL on purpose. A second concurrent full-suite process on the
# same host perturbs thread scheduling, which is exactly the variable under test.
#
# Usage: ws_relay_final_verify.sh <label> <python> <runs>
#   e.g. ws_relay_final_verify.sh p313 ~/miniconda3/envs/p313/bin/python 2
set -u
LABEL="$1"
PY="$2"
RUNS="${3:-2}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="${FLAKE_OUT:-/tmp/verify_$LABEL}"
mkdir -p "$OUT"

# Worker-like env, as the dispatcher runs it: HERMES_HOME points at a PROFILE dir.
export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes/profiles/backend-engineer}"

COMMIT="$(git -C "$ROOT" rev-parse HEAD)"
DIRTY="$(git -C "$ROOT" status --porcelain | wc -l | tr -d ' ')"
echo "verify: label=$LABEL commit=$COMMIT dirty_files=$DIRTY runs=$RUNS"
echo "verify: interpreter=$("$PY" -V 2>&1) HERMES_HOME=$HERMES_HOME"
[ "$DIRTY" = "0" ] || echo "verify: WARNING worktree is DIRTY — green does not describe a commited tip"

rc_all=0
for i in $(seq 1 "$RUNS"); do
  log="$OUT/${LABEL}_run${i}.log"
  {
    echo "===== HSCC FINAL VERIFICATION ($LABEL) ====="
    echo "commit:      $COMMIT"
    echo "branch:      $(git -C "$ROOT" rev-parse --abbrev-ref HEAD)"
    echo "dirty_files: $DIRTY"
    echo "interpreter: $("$PY" -V 2>&1)"
    echo "pytest:      $("$PY" -m pytest --version 2>&1 | head -1)"
    echo "HERMES_HOME: $HERMES_HOME"
    echo "started:     $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    echo "==========================================="
  } > "$log"
  HSCC_TEST_PY="$PY" bash "$ROOT/scripts/run_tests.sh" >> "$log" 2>&1
  rc=$?
  {
    echo "ended:  $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    echo "rc:     $rc"
    echo "--- per-suite results ---"
    awk '/^━━━ /{dir=$2} /^[0-9]+ (passed|failed)/{printf "  %-16s %s\n", dir, $0}' "$log"
    echo "--- target file (must never appear above as FAILED) ---"
    grep -cE "FAILED.*(test_ws_relay_not_noop|test_ws_stop_noop_isolation)" "$log" \
      | sed 's/^/  target-file failures: /'
  } >> "$log"
  passed=$(grep -oE "[0-9]+ passed" "$log" | awk '{s+=$1} END {print s+0}')
  failed=$(grep -cE "^FAILED|^ERROR " "$log")
  echo "  ${LABEL} run$i rc=$rc passed=$passed failed=$failed -> $log"
  [ $rc -ne 0 ] && rc_all=1
done
echo "verify $LABEL: rc_all=$rc_all"
exit $rc_all
