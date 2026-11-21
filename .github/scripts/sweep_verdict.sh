#!/usr/bin/env bash
# Classify ONE alone-vs-in-leg sweep's exit code for CI (t_4447d736).
#
# WHY THIS IS A SCRIPT. Same reason as leg_verdict.sh, which this mirrors: the
# three-way split (measured-green / measured-red / NOT MEASURED) is the whole
# value of the gate, and inline YAML is where "advisory" quietly starts swallowing
# a run that never happened. docs/audits/daemon-alone-run-order-dependence_t_572b4856.md
# §11 is why this one exists at all: the sweep has now been taken on the CI
# posture, and a job that printed green over a sweep that measured nothing would
# be the exact hole that audit is about.
#
# CONTRACT (the exit codes docs/audits/support/t_572b4856/alone_sweep.sh emits):
#   0  every swept file answers alone exactly as it does in-leg
#                                  -> note, green
#   1  a file answers alone differently, OR the leg it swept is red
#                                  -> ADVISORY: warning + green; red when
#                                     HSCC_TESTS_ENFORCE is on. Both halves are
#                                     advisory-by-design here: a red leg is not an
#                                     order effect, and the sweep's own log says
#                                     which half it is ("the LEG ITSELF is red").
#   2  no per-file junit xml — nothing was measured
#   99 bad invocation / repo inference wrong
#   *  anything else (a kill, a signal)
#                                  -> RED either way. "Could not run" is never
#                                     advisory, and rc 0 without a RESULT line is
#                                     treated the same way: a sweep that exited 0
#                                     without ever printing a verdict did not
#                                     measure the property.
set -uo pipefail

rc="${1:?usage: sweep_verdict.sh <alone_sweep.sh exit code> <log file>}"
log="${2:?usage: sweep_verdict.sh <alone_sweep.sh exit code> <log file>}"

enforcelc="$(printf '%s' "${ENFORCE:-}" | tr '[:upper:]' '[:lower:]')"
if [ "${FORCED_ENFORCE:-}" = "true" ]; then
  enforcelc=true
fi

if [ "$rc" -eq 0 ]; then
  # A zero exit with no verdict line means the sweep never reached its diff — the
  # same "green because nothing ran" shape this workflow refuses everywhere else.
  if ! grep -q '^RESULT: ' "$log" 2>/dev/null; then
    echo "::error::the sweep exited 0 but printed no RESULT line — it did not measure the property"
    exit 1
  fi
  echo "sweep: green — alone == in-leg for every file swept"
  exit 0
fi

if [ "$rc" -eq 2 ] || [ "$rc" -eq 99 ]; then
  echo "::error::the sweep could not run (exit ${rc}) — this is not a test result, and it is not advisory"
  exit 1
fi

if [ "$rc" -ne 1 ]; then
  echo "::error::the sweep exited ${rc}, which is not one of alone_sweep.sh's verdict codes"
  exit 1
fi

if ! grep -q '^RESULT: ' "$log" 2>/dev/null; then
  echo "::error::the sweep exited 1 but printed no RESULT line — the run is uninterpretable"
  exit 1
fi

case "$enforcelc" in
  true | 1 | yes)
    echo "::error::alone != in-leg (or the swept leg is red) and HSCC_TESTS_ENFORCE is on — see the summary"
    exit 1
    ;;
esac

echo "::warning::alone != in-leg (or the swept leg is red) — advisory: HSCC_TESTS_ENFORCE is not set to true. Names are in the step summary."
exit 0
