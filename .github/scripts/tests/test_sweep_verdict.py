"""The advisory/blocking verdict for one alone-vs-in-leg sweep (t_4447d736).

Same shape as ``test_leg_verdict.py``, same reason for existing: the three-way
split (measured-green / measured-red / NOT MEASURED) is the entire value of the
instrument, and inline YAML is where "advisory" quietly starts meaning "we did
not look". The audit this job came out of
(``docs/audits/daemon-alone-run-order-dependence_t_572b4856.md`` §11) is a write-up
of what happens when a leg reports green over something that never ran, so the
branches that keep "could not run" out of the advisory lane are executed here
rather than asserted in a comment.

``alone_sweep.sh``'s own exit codes are the input contract: 0 agreement, 1 a file
answers alone differently (or the leg it swept is red), 2 no per-file junit xml,
99 bad invocation.
"""

import os
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "sweep_verdict.sh"

AGREEMENT = "# files swept: 131   files whose ALONE run differs from the leg: 0\n" \
            "RESULT: alone == in-leg for every file swept.\n"
DISAGREEMENT = "test_board_pin: in-leg (18, 0, 0, 2) | alone (18, 0, 0, 2) | rc=1\n" \
               "# files swept: 131   files whose ALONE run differs from the leg: 1\n" \
               "RESULT: alone != in-leg for at least one file (see above).\n"
RED_LEG = "RESULT: the LEG ITSELF is red — fix that before reading the diff.\n"


def _verdict(tmp_path: Path, rc: int, log: str, **env: str) -> subprocess.CompletedProcess:
    logfile = tmp_path / "sweep.log"
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


def test_agreement_is_green(tmp_path):
    proc = _verdict(tmp_path, 0, AGREEMENT)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "green" in proc.stdout


def test_a_disagreement_is_advisory_by_default(tmp_path):
    proc = _verdict(tmp_path, 1, DISAGREEMENT)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "::warning::" in proc.stdout


def test_a_red_leg_swept_is_advisory_too_because_it_is_not_an_order_effect(tmp_path):
    # alone_sweep.sh collapses both halves of rc 1 on purpose; the log says which,
    # and the step summary carries the names either way.
    proc = _verdict(tmp_path, 1, RED_LEG)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "::warning::" in proc.stdout


def test_enforcement_turns_the_disagreement_red(tmp_path):
    for value in ("true", "True", "TRUE", "1", "yes"):
        proc = _verdict(tmp_path, 1, DISAGREEMENT, ENFORCE=value)
        assert proc.returncode == 1, f"{value!r} stayed advisory: {proc.stdout}"
        assert "::error::" in proc.stdout


def test_the_per_run_override_enforces_the_same_way(tmp_path):
    proc = _verdict(tmp_path, 1, DISAGREEMENT, FORCED_ENFORCE="true")
    assert proc.returncode == 1, proc.stdout


def test_nothing_measured_is_never_advisory(tmp_path):
    # rc 2 = alone_sweep.sh found no per-file junit xml. A green here would be the
    # exact "green because nothing ran" hole this file's parent audit documents.
    proc = _verdict(tmp_path, 2, "")
    assert proc.returncode == 1
    assert "::error::" in proc.stdout


def test_a_bad_invocation_is_never_advisory(tmp_path):
    proc = _verdict(tmp_path, 99, "alone_sweep.sh: no scripts/run_tests.sh under '' — repo inference is wrong\n")
    assert proc.returncode == 1
    assert "::error::" in proc.stdout


def test_a_signal_is_never_advisory(tmp_path):
    # A killed sweep (the 40-minute job ceiling, an OOM) is not a test result.
    proc = _verdict(tmp_path, 137, DISAGREEMENT)
    assert proc.returncode == 1
    assert "::error::" in proc.stdout


def test_rc_zero_without_a_verdict_line_is_not_green(tmp_path):
    # The anti-vacuity tooth: exit 0 with no `RESULT:` means the sweep never
    # reached its diff. Advisory-green here would report an agreement nobody took.
    proc = _verdict(tmp_path, 0, "== alone sweep (one process per file)\n")
    assert proc.returncode == 1
    assert "no RESULT line" in proc.stdout


def test_rc_one_without_a_verdict_line_is_an_error_not_a_warning(tmp_path):
    proc = _verdict(tmp_path, 1, "Traceback (most recent call last)\n")
    assert proc.returncode == 1
    assert "::error::" in proc.stdout
