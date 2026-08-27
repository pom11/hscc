#!/bin/bash
# flake_hunt.sh — reproduce an intermittent test failure, and never lose the name.
#
# Why this exists: a suite run once reported a failure that did not reproduce.
# Only the last few lines of output had been kept, so the FAILING TEST NAME was
# gone before it could be investigated. Re-running destroys the evidence.
#
# Runs the full suite N times, keeps the COMPLETE output of every run, and prints
# the failing test names for any run that fails.
#
# It also hashes live operator state before and after EACH run. A test that
# mutates the operator's real config is worse than a flake, and every live-state
# leak in this project was caught this way rather than by a test.
#
# Usage:  bash scripts/flake_hunt.sh [runs]        (default 6)
# Output: /tmp/flake/run<N>.log — full output, one file per run.

set -u
RUNS="${1:-6}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${FLAKE_OUT:-/tmp/flake}"
PY="${HSCC_TEST_PY:-python3}"
mkdir -p "$OUT"
cd "$REPO" || exit 1

# Hash only the STATE files: the daemon writes logs into ~/.hscc constantly, so
# hashing the whole directory reports a change on every run and means nothing.
snap_state() {
  for f in autodown.json serving.json api.json; do
    shasum "$HOME/.hscc/$f" 2>/dev/null
  done | shasum | awk '{print $1}'
}
snap_profiles() {
  find "$HOME/.hermes/profiles" -name config.yaml -exec shasum {} \; 2>/dev/null \
    | shasum | awk '{print $1}'
}

failures=0
for i in $(seq 1 "$RUNS"); do
  before_state=$(snap_state); before_profiles=$(snap_profiles)
  HSCC_TEST_PY="$PY" scripts/run_tests.sh > "$OUT/run$i.log" 2>&1
  rc=$?
  after_state=$(snap_state); after_profiles=$(snap_profiles)

  failed=$(grep -cE "^FAILED|^ERROR " "$OUT/run$i.log")
  passed=$(grep -oE "[0-9]+ passed" "$OUT/run$i.log" | awk '{s+=$1} END {print s+0}')

  leak=""
  [ "$before_state" != "$after_state" ] && leak="${leak}hscc-state "
  [ "$before_profiles" != "$after_profiles" ] && leak="${leak}profiles "

  echo "run$i rc=$rc passed=$passed failed=$failed leak=${leak:-none}"

  # passed=0 means the suite never really ran — a green result that proves nothing.
  [ "$passed" = "0" ] && echo "  WARNING: no tests ran; treat this run as invalid"

  if [ "$failed" != "0" ] || [ $rc -ne 0 ]; then
    failures=$((failures + 1))
    echo "  --- failing tests (full output: $OUT/run$i.log) ---"
    grep -E "^FAILED|^ERROR " "$OUT/run$i.log" | head -20
  fi
  if [ -n "$leak" ]; then
    echo "  --- LIVE-STATE LEAK: a test wrote to the operator's real config ---"
    echo "      This outranks the flake. Find the test before running anything else."
  fi
done

echo "flake_hunt: $failures of $RUNS runs failed"
