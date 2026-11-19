"""The shape of .github/workflows/pytest-legs.yml (t_23bf61f3).

A workflow file is load-bearing configuration that nothing else reads: if someone
drops the `pull_request` trigger, no test anywhere notices and the PR head stops
being checked — the exact silence that let #443/#446 merge with hscc-api red. The
existing jobs have the same exposure; this one is the first to be pinned, because
it is the one whose whole job is to block things.

Parsed by an indent walk over the few lines that matter rather than by a YAML
library: `.github/scripts` is a leg that must run under a THIN interpreter (the
stamp's sibling env carries pytest and nothing else), and the micro-notify lesson
is that a hand-rolled walk over the keys you actually care about is both dep-free
and less surprising than a full loader that reads bare `on:` as boolean True.

What is pinned, in order of the damage each prevents:

  1. both `push` and `pull_request` triggers. A workflow that triggers on push
     only never runs on a PR head, so a required check built on it blocks every
     PR forever;
  2. exactly one job, and the check name branch protection has to be typed by
     hand ("pytest-legs / legs") is derived from `name:` + job id, so both are
     pinned;
  3. the enforcement switch is the repository VARIABLE (vars.HSCC_TESTS_ENFORCE),
     not a literal in this file — the operator's advisory/blocking decision must
     not be editable by a diff;
  4. the fail-closed couplets: the leg list produced by the decide step is the
     only thing the run step consumes, and the non-test-failure exit codes are
     gated BEFORE the advisory branch is reached.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO / ".github" / "workflows"
WF = WORKFLOWS / "pytest-legs.yml"


def _lines():
    return WF.read_text(encoding="utf-8").splitlines()


def _top_keys():
    return [ln[:-1] for ln in _lines() if re.match(r"^[A-Za-z0-9_.-]+:$", ln)]


def _block_keys(key: str) -> list[str]:
    """The immediate children of a top-level block, by indent."""
    out, inside = [], False
    for ln in _lines():
        if re.match(rf"^{re.escape(key)}:$", ln):
            inside = True
            continue
        if inside:
            if ln and not ln.startswith((" ", "\t", "#")):
                break
            m = re.match(r"^  ([A-Za-z0-9_.-]+):\s*$", ln)
            if m:
                out.append(m.group(1))
    return out


def test_the_file_is_where_a_leg_is_supposed_to_live():
    # The premise of the card, restated as a regression guard: grep of the
    # workflows dir used to match nothing for run_tests.
    assert WF.exists()
    assert "scripts/run_tests.sh" in WF.read_text(encoding="utf-8")
    hits = [p.name for p in WORKFLOWS.glob("*.yml") if "run_tests.sh" in p.read_text(encoding="utf-8")]
    assert hits == ["pytest-legs.yml"], f"expected exactly one workflow to run the roster, got {hits}"


def test_both_pr_and_push_triggers_stay():
    """Without pull_request the check never runs on a PR head; without push the
    full-suite backstop on main disappears. Both are load-bearing (file header)."""
    assert sorted(_block_keys("on")) == sorted(["push", "pull_request", "workflow_dispatch"])


def test_exactly_one_job_and_the_check_name_branch_protection_types():
    jobs = _block_keys("jobs")
    assert jobs == ["legs"], (
        f"branch protection requires the check by name ('pytest-legs / legs'); "
        f"the job set is now {jobs}"
    )
    assert re.search(r"^name: pytest-legs$", WF.read_text(encoding="utf-8"), re.MULTILINE)


def test_the_run_step_is_wired_to_the_decide_step_and_not_to_a_literal():
    text = WF.read_text(encoding="utf-8")
    assert "id: legs" in text
    assert 'LEGS: ${{ steps.legs.outputs.legs }}' in text, (
        "the run step must consume the selector's output; a literal leg list here "
        "re-creates the 'green because nothing ran' problem inside the gate"
    )
    assert "HSCC_TEST_LEGS=" in text


def test_enforcement_is_a_repository_variable_and_fails_open_on_nothing():
    text = WF.read_text(encoding="utf-8")
    assert "${{ vars.HSCC_TESTS_ENFORCE }}" in text, (
        "advisory-vs-blocking must be an operator setting, not an edit to this file "
        "(same shape as address-guard.yml's ADDRESS_GUARD_ENFORCE)"
    )
    # A variable named ENFORCE is never allowed to fail OPEN on capitalisation.
    assert re.search(r"case \"\$enforcelc\" in\s+true\|1\|yes\)", text), (
        "the enforce test must normalise case and accept 1/yes, so 'True' cannot "
        "silently downgrade the job to advisory"
    )
    # ...and the harness-broken case is decided BEFORE the advisory branch.
    harness = text.index("pytest exit (0|[2-9]|1[0-9])")
    advisory = text.index("::warning::a test leg is red")
    assert harness < advisory, "a leg that could not run must not reach advisory mode"


def test_the_legs_job_does_not_cancel_a_push_to_main():
    text = WF.read_text(encoding="utf-8")
    m = re.search(r"cancel-in-progress: (.+)", text)
    assert m and "pull_request" in m.group(1), (
        "cancelling a full-suite run on main destroys the backstop, and a "
        "cancelled check counts against branch protection"
    )


def test_no_other_workflow_silently_lost_its_triggers():
    """Cheap neighbourhood guard: the two older jobs keep push+pull_request."""
    for name in ("address-guard.yml", "app-build-checks.yml"):
        text = (WORKFLOWS / name).read_text(encoding="utf-8")
        assert "\n  push:\n" in text and "\n  pull_request:\n" in text, f"{name} lost a trigger"
