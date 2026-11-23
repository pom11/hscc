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
