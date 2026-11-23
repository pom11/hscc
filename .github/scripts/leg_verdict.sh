#!/usr/bin/env bash
# Decide one leg job's verdict from scripts/run_tests.sh's exit code (t_23bf61f3).
#
# WHY THIS IS A SCRIPT. The job must keep the same advisory-then-blocking shape
# as address-guard.yml (one repository variable, HSCC_TESTS_ENFORCE), and it must
# never let a harness failure hide behind "advisory". Both are easy to get wrong
# in inline YAML — the first draft of this gate put `rc=$?` after the runner
# under the runner's own `bash -e`, where it never executed, and matched only
# `pytest exit 1x`, where a SIGKILLed leg reports 143. This scripts it and
# .github/scripts/tests/test_leg_verdict.py executes every branch.
#
# CONTRACT:
#   rc 0                          -> green
#   rc 64                         -> red (run_tests.sh rejected the selection)
#   "pytest exit N" with N != 1   -> red (the harness, not a test, is broken)
#   rc 1, a real test failure    -> advisory: warning + green; enforced: red
#   HSCC_TESTS_ENFORCE=true/1/yes -> enforced (case-insensitive, never fail-open)
#   FORCED_ENFORCE=true           -> enforced for this run (workflow_dispatch)
set -uo pipefail

rc="${1:?usage: leg_verdict.sh <run_tests.sh exit code> <log file>}"
log="${2:?usage: leg_verdict.sh <run_tests.sh exit code> <log file>}"

enforcelc="$(printf '%s' "${ENFORCE:-}" | tr '[:upper:]' '[:lower:]')"
if [ "${FORCED_ENFORCE:-}" = "true" ]; then
  enforcelc=true
fi

if [ "$rc" -eq 0 ]; then
  echo "leg: green"
  exit 0
fi

if [ "$rc" -eq 64 ]; then
  echo "::error::scripts/run_tests.sh rejected the leg selection (exit 64) — nothing ran"
  exit 64
fi

# A run that died before the summary did not fail a test; it failed to finish.
# Treating a truncated log as advisory would hide a killed leg behind a warning.
if ! grep -q '━━━ Summary ━━━' "$log"; then
  echo "::error::the leg produced no summary — the runner died mid-leg (not a test failure)"
  exit 1
fi

# run_tests.sh returns 1 for "some leg failed" and 64 for a rejected selection.
# Any other code is the harness, not a test failure.
if [ "$rc" -ne 1 ]; then
  echo "::error::scripts/run_tests.sh exited ${rc}, which is not a test-failure code"
  exit 1
fi

# run_tests.sh flattens every leg failure to rc=1 and keeps pytest's own code in
# the summary. 0 or any N != 1 (collection error, usage error, no tests
# collected, killed) means the harness, not a test, is broken: red either way.
if grep -Eq 'pytest exit (0|[2-9]|[1-9][0-9]+)' "$log"; then
  echo "::error::a leg exited with a non-test-failure code (see the summary above) — the harness, not a test, is broken"
  exit 1
fi

case "$enforcelc" in
  true | 1 | yes)
    echo "::error::a test leg is red (HSCC_TESTS_ENFORCE is on) — see the summary above"
    exit "$rc"
    ;;
esac

echo "::warning::a test leg is red (advisory: HSCC_TESTS_ENFORCE is not set to true) — see the summary above"
exit 0
