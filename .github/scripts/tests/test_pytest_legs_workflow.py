"""The shape of .github/workflows/pytest-legs.yml (t_23bf61f3).

A workflow file is load-bearing configuration that nothing else reads: if someone
drops the `pull_request` trigger, no test anywhere notices and the PR head stops
being checked — the exact silence that let #443/#446 merge with hscc-api red. The
existing jobs have the same exposure; this one is the first to be pinned, because
it is the one whose whole job is to block things.

Parsed by an indent walk over the few lines that matter rather than by a YAML
library: `.github/scripts` is a leg that must run under a THIN interpreter (the
stamp's sibling env carries pytest and nothing else), so a PyYAML import here
would make the gate's own tests depend on a package the CI posture does not
guarantee.

What is pinned, in order of the damage each prevents:

  1. both `push` (main only) and `pull_request` triggers. A workflow that
     triggers on push only never runs on a PR head, so a required check built on
     it blocks every PR forever; all-branch push would pay for a duplicate run on
     every PR commit and would make the main-only backstop ambiguous;
  2. the stable check name branch protection has to type by hand
     ("pytest-legs / legs") is the final job, and it always runs so a failed
     matrix cannot leave the required check missing;
  3. one matrix leg per leg, in parallel — the roster must not become the sum of
     its durations;
  4. the enforcement switch is the repository VARIABLE (vars.HSCC_TESTS_ENFORCE),
     not a literal in this file — the operator's advisory/blocking decision must
     not be editable by a diff;
  5. the fail-closed couplets live in tested scripts, not inline YAML: push ->
     full roster is decide_legs.sh, and "could not run is red before advisory"
     is leg_verdict.sh;
  6. `-e` is disabled around the runner, because GitHub's `bash -e` would abort
     the step before rc=$? and turn a classified red into an opaque error.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO / ".github" / "workflows"
WF = WORKFLOWS / "pytest-legs.yml"


def _text():
    return WF.read_text(encoding="utf-8")


def _lines():
    return _text().splitlines()


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
    assert "scripts/run_tests.sh" in _text()
    hits = [p.name for p in WORKFLOWS.glob("*.yml") if "run_tests.sh" in p.read_text(encoding="utf-8")]
    assert hits == ["pytest-legs.yml"], f"expected exactly one workflow to run the roster, got {hits}"


def test_both_pr_and_push_triggers_stay():
    """Without pull_request the check never runs on a PR head; without push the
    full-suite backstop on main disappears. Both are load-bearing (file header)."""
    assert sorted(_block_keys("on")) == sorted(["push", "pull_request", "workflow_dispatch"])


def test_push_is_main_only():
    """`push:` on every branch doubles every PR run and blurs where the
    full-suite backstop lives; decide_legs.sh decides push -> all legs."""
    text = _text()
    assert "  push:\n    branches: [main]\n" in text


def test_the_final_gate_has_the_check_name_branch_protection_types():
    jobs = _block_keys("jobs")
    # `alone-sweep` (t_4447d736) is inserted BEFORE `legs` on purpose: the required
    # check branch protection types stays the last job in the file, and the advisory
    # baseline never becomes part of it (pinned by
    # test_the_advisory_sweep_is_not_in_the_required_checks_chain).
    assert jobs == ["select", "leg", "alone-sweep", "legs"], (
        "branch protection requires the check by name ('pytest-legs / legs'); "
        f"the job set is now {jobs}"
    )
    assert re.search(r"^  legs:\n    name: legs$", _text(), re.MULTILINE)
    assert "    if: always()" in _text(), (
        "the verdict job must run even when a matrix leg fails, or the required "
        "check disappears instead of going red"
    )


def test_one_parallel_job_per_leg_from_the_selector_output():
    text = _text()
    assert "include: ${{ fromJSON(needs.select.outputs.matrix) }}" in text, (
        "the matrix must consume the selector's JSON objects, not a literal leg list"
    )
    assert "fail-fast: false" in text, "one red leg must not cancel the others"
    assert "bash .github/scripts/decide_legs.sh" in text
    assert "bash .github/scripts/leg_verdict.sh \"$rc\" \"$log\"" in text


def test_the_runner_is_invoked_with_the_matrix_leg_only():
    text = _text()
    assert 'HSCC_TEST_LEGS="$LEG"' in text
    assert "HSCC_TEST_PY:" in text, "the cached venv must be the interpreter the leg runs under"


def test_enforcement_is_a_repository_variable_with_a_per_run_override():
    text = _text()
    assert "${{ vars.HSCC_TESTS_ENFORCE }}" in text, (
        "advisory-vs-blocking must be an operator setting, not an edit to this file "
        "(same shape as address-guard.yml's ADDRESS_GUARD_ENFORCE)"
    )
    assert "FORCED_ENFORCE: ${{ github.event.inputs.enforce }}" in text


def test_a_failing_runner_cannot_abort_the_step_before_it_is_classified():
    """GitHub runs `bash -e`; with -e on, `rc=$?` after a failing runner never
    executes. The verdict script would never be reached."""
    text = _text()
    run = text.index("bash scripts/run_tests.sh")
    assert "set +e" in text[:run], "no `set +e` before the runner: rc=$? is dead code"
    assert text.index("set +e") < run


def test_the_venv_is_cached_and_the_pip_cache_is_kept():
    text = _text()
    assert "actions/cache@v4" in text
    assert "hashFiles('requirements.txt', 'requirements-ci.txt')" in text
    assert "cache: pip" in text
    assert "-r requirements-ci.txt" in text, (
        "the test-only imports (websockets, mcp, sqlalchemy, numpy, Pillow, "
        "setuptools) live in requirements-ci.txt; installing requirements.txt "
        "alone makes four legs die at COLLECTION on the runner"
    )


def test_each_leg_job_runs_on_the_platform_the_selector_chose():
    """The first ubuntu run died collecting hscc_daemon on select.kqueue; the
    selector marks that one macos-latest and leaves the rest on the 20-slot
    ubuntu pool (pinned in test_leg_select.py). The workflow must consume it."""
    text = _text()
    leg_job = text[text.index("  leg:\n") : text.index("\n  legs:\n    name: legs")]
    assert "    runs-on: ${{ matrix.os }}" in leg_job
    assert "include: ${{ fromJSON(needs.select.outputs.matrix) }}" in leg_job, (
        "a named axis would make matrix.leg the whole {leg, os} object and leave "
        "matrix.os unset, which queues no leg job at all"
    )


def test_the_scripts_leg_checks_out_full_history():
    """The scripts leg reconstructs the pre-migration CHANGELOG.md from
    ``git log`` (test_changelog_fragments.py). actions/checkout defaults to
    depth 1, which made that test red on Ubuntu while it passed in a full
    local clone; only the scripts leg needs the history."""
    text = _text()
    leg_job = text[text.index("  leg:\n") : text.index("\n  legs:\n    name: legs")]
    assert "fetch-depth: ${{ matrix.leg != 'scripts' && 1 || 0 }}" in leg_job, (
        "the scripts leg needs full git history; every other leg stays shallow "
        "(note: GitHub expressions treat 0 as falsy, so the ternary is inverted)"
    )


def test_the_legs_job_does_not_cancel_a_push_to_main():
    text = _text()
    m = re.search(r"cancel-in-progress: (.+)", text)
    assert m and "pull_request" in m.group(1), (
        "cancelling a full-suite run on main destroys the backstop, and a "
        "cancelled check counts against branch protection"
    )


def _sweep_job() -> str:
    """The `alone-sweep` job block (t_4447d736), up to the required-check job."""
    text = _text()
    return text[text.index("  alone-sweep:\n") : text.index("\n  legs:\n    name: legs")]


def test_the_advisory_sweep_is_dispatch_gated_and_sequential():
    """Two properties, both about cost and both easy to lose in a later edit: it
    must not run unless a human asked (a 131-process sweep on every PR would be
    clicked through, which is worse than no gate), and it must not run alongside
    the leg it compares against — the sweep's own rule is one pytest process per
    file over one checkout."""
    job = _sweep_job()
    assert "needs: leg" in job, "without needs the sweep races the leg matrix"
    assert "workflow_dispatch" in job and "alone_sweep == 'true'" in job
    assert "\n      alone_sweep:\n" in _text(), "the gate needs its dispatch input"
    assert "runs-on: macos-latest" in job, "hscc_daemon cannot be collected on ubuntu"
    assert "timeout-minutes:" in job, "a hung pytest must not burn a macOS runner for 6 h"


def test_the_advisory_sweep_measures_the_ci_posture_and_not_a_fourth_one():
    """The whole point of the job is to answer "is the agreement property green in
    THE CI environment" — so it must reuse the legs' interpreter, pins, cache key
    and venv path, and it must delegate the actual measurement to the one script
    the nightly gate uses (a second implementation is how a gate starts measuring
    something adjacent)."""
    job = _sweep_job()
    assert 'python-version: "3.13"' in job
    assert "PY: ${{ github.workspace }}/.venv/bin/python" in job
    assert "-r requirements-ci.txt" in job
    assert "test-venv-${{ runner.os }}" in job
    assert "bash docs/audits/support/t_572b4856/alone_sweep.sh" in job


def test_the_advisory_sweep_cannot_classify_itself():
    job = _sweep_job()
    assert "set +e" in job, "GitHub's bash -e would kill the step before rc=$?"
    assert 'bash .github/scripts/sweep_verdict.sh "$rc" "$log"' in job, (
        "the three-way split lives in the tested script, not in YAML"
    )
    assert "actions/upload-artifact@v4" in job, (
        "the diff names files; only the per-file xml and -rs logs hold the reasons"
    )


def test_the_advisory_sweep_is_not_in_the_required_checks_chain():
    """An advisory baseline that the required verdict job waits on is a blocking
    gate wearing a warning annotation. The sweep may inform the operator; it must
    not be able to turn `pytest-legs / legs` red."""
    legs = _text()[_text().index("\n  legs:\n    name: legs"):]
    assert "needs: [select, leg]" in legs
    assert "alone-sweep" not in legs, (
        "the required check must not depend on the advisory sweep (t_4447d736)"
    )


def test_no_other_workflow_silently_lost_its_triggers():
    """Cheap neighbourhood guard: the two older jobs keep push+pull_request."""
    for name in ("address-guard.yml", "app-build-checks.yml"):
        text = (WORKFLOWS / name).read_text(encoding="utf-8")
        assert "\n  push:\n" in text and "\n  pull_request:\n" in text, f"{name} lost a trigger"
