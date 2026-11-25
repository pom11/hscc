"""The advisory/blocking verdict for one leg (t_23bf61f3).

Every branch is executed against the shipped script. Two of them are the ones
that made the first draft of this gate decorative:

  * GitHub runs steps under `bash -e`, so `rc=$?` after a failing runner never
    executes — a red leg became an opaque step error and the advisory branch was
    unreachable. That is why the workflow has `set +e` (pinned in
    test_pytest_legs_workflow.py) and why the classification lives here.
  * run_tests.sh flattens a SIGKILLed leg (pytest exit 143) to rc=1. Only the
    summary knows it was not a test failure; matching just `pytest exit 1x`
    would have reported it advisory-green.
"""

import os
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "leg_verdict.sh"

SUMMARY = "━━━ Summary ━━━\n  ✗ hscc-api (pytest exit {code})\nFAILURES ABOVE\n"


def _verdict(tmp_path: Path, rc: int, log: str, **env: str) -> subprocess.CompletedProcess:
    logfile = tmp_path / "leg.log"
    logfile.write_text(log, encoding="utf-8")
    base = {"PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    base.update(env)
    return subprocess.run(
        ["bash", str(SCRIPT), str(rc), str(logfile)],
        capture_output=True,
        text=True,
        check=False,
        env=base,
    )


def test_green_leg_is_green(tmp_path):
    proc = _verdict(tmp_path, 0, "━━━ Summary ━━━\n  ✓ hscc-api\nALL GREEN\n")
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_a_real_test_failure_is_advisory_by_default(tmp_path):
    proc = _verdict(tmp_path, 1, SUMMARY.format(code=1))
    assert proc.returncode == 0, "advisory mode must not fail the check"
    assert "::warning::" in proc.stdout
    assert "::error::" not in proc.stdout


def test_a_capitalised_enforce_variable_blocks(tmp_path):
    proc = _verdict(tmp_path, 1, SUMMARY.format(code=1), ENFORCE="True")
    assert proc.returncode == 1
    assert "::error::" in proc.stdout


def test_per_run_enforce_input_blocks_with_the_variable_unset(tmp_path):
    proc = _verdict(tmp_path, 1, SUMMARY.format(code=1), FORCED_ENFORCE="true")
    assert proc.returncode == 1


def test_a_collection_error_is_red_even_in_advisory(tmp_path):
    proc = _verdict(tmp_path, 1, SUMMARY.format(code=2))
    assert proc.returncode == 1, "pytest exit 2 is a harness break, not a test failure"
    assert "::error::" in proc.stdout


def test_a_killed_leg_is_red_even_in_advisory(tmp_path):
    # rc=1 is what run_tests.sh reports for SIGTERM/SIGKILL; pytest's own code
    # survives in the summary as 143.
    proc = _verdict(tmp_path, 1, SUMMARY.format(code=143))
    assert proc.returncode == 1


# ── the class that used to be indistinguishable from a red test (t_f508ce51) ──
#
# A packed leg whose wrapper died before it could write its `.rc` record, and whose
# log holds nothing, has NO evidence about it at all. Before run_tests.sh said so on
# the row, that leg reported `pytest exit 1` — this script's own definition of an
# ordinary TEST failure, i.e. `::warning::` and a GREEN check. The shape below is what
# the runner emits for it now (byte-compatible with the digest readers; see
# scripts/tests/test_run_tests_leg_harness_verdict.py for the runner side).

NO_LEG = ("━━━ Summary ━━━\n"
          "  ✗ hscc-api (no leg output — harness failure, pytest exit 1, 0s)\n"
          "FAILURES ABOVE\n")


def test_a_leg_the_harness_could_not_run_is_red_even_in_advisory(tmp_path):
    """THE hole this closes: rc 1 + `pytest exit 1` used to mean advisory green."""
    proc = _verdict(tmp_path, 1, NO_LEG)
    assert proc.returncode == 1, (
        "a leg that produced no record and no output is never advisory — it never ran")
    assert "::error::" in proc.stdout, proc.stdout
    assert "::warning::" not in proc.stdout, proc.stdout


def test_the_harness_verdict_is_still_red_when_enforced(tmp_path):
    proc = _verdict(tmp_path, 1, NO_LEG, ENFORCE="true")
    assert proc.returncode == 1
    assert "::error::" in proc.stdout


def test_the_phrase_only_counts_on_a_summary_row(tmp_path):
    """The anchor has teeth in the fail-towards-test direction too.

    A genuine test failure whose own output happens to contain the words must stay
    ADVISORY: reclassifying a real red as "the harness" would make every flake into a
    blocking infrastructure failure and teach people to ignore the verdict.
    """
    log = ("━━━ hscc-api ━━━\n"
           "FAILED hscc-api/tests/test_stub.py::test_quotes - assert 'no leg output' == ''\n"
           + SUMMARY.format(code=1))
    proc = _verdict(tmp_path, 1, log)
    assert proc.returncode == 0, (
        f"a real test failure was reclassified as the harness:\n{proc.stdout}")
    assert "::warning::" in proc.stdout, proc.stdout


def test_a_truncated_run_is_red_even_in_advisory(tmp_path):
    proc = _verdict(tmp_path, 1, "━━━ hscc-api ━━━\n")
    assert proc.returncode == 1
    assert "no summary" in proc.stdout


def test_the_runner_rejecting_the_selection_is_red_even_in_advisory(tmp_path):
    proc = _verdict(tmp_path, 64, "")
    assert proc.returncode == 64
    assert "::error::" in proc.stdout


def test_advisory_does_not_swallow_a_nonzero_rc_without_a_summary(tmp_path):
    # Belt and braces on the fail-closed direction: rc != 0 can never exit 0
    # unless the log positively identifies it as a plain pytest-1 failure.
    for rc in (2, 3, 255):
        proc = _verdict(tmp_path, rc, SUMMARY.format(code=1), ENFORCE="")
        assert proc.returncode != 0, f"rc={rc} was reported advisory-green"
