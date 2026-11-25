"""The shape of .github/workflows/app-build-checks.yml and its selector (t_31679ad1).

The heavy Apple compile used to run on macos-latest for every push and
pull_request. Its two load-bearing decisions must be executable, not prose:

  * which diffs need a compile is .github/scripts/decide_app_build.sh, executed
    here against throwaway git repositories — including the "main moved after
    the branch point" shape, where a two-dot diff would reverse main's files
    into the PR and compile forever;
  * the required check name `compile` reports on every PR and fails closed when
    the compile was required but did not run. The verdict step is extracted from
    the YAML and EXECUTED over the (changes, apps, apple) truth table, because a
    text pin cannot tell a green-on-skip from a green-on-failure.

Stdlib only, like the sibling workflow pins: .github/scripts is a leg that runs
under a thin interpreter without PyYAML.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
CI_SCRIPTS = HERE.parent
REPO = CI_SCRIPTS.parents[1]
WF = REPO / ".github" / "workflows" / "app-build-checks.yml"
DECIDE = CI_SCRIPTS / "decide_app_build.sh"


def _text() -> str:
    return WF.read_text(encoding="utf-8")


def _jobs() -> dict[str, str]:
    """job name -> block text, by the two-space keys under `jobs:`."""
    lines = _text().splitlines()
    start = lines.index("jobs:") + 1
    blocks: dict[str, list[str]] = {}
    key = None
    for line in lines[start:]:
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if m:
            key = m.group(1)
            blocks[key] = []
        elif key is not None:
            blocks[key].append(line)
    return {name: "\n".join(block) for name, block in blocks.items()}


def _run_block(job: str) -> str:
    """The first `run: |` body of a job, dedented, ready for bash."""
    lines = job.splitlines()
    for i, line in enumerate(lines):
        if line.strip() != "run: |":
            continue
        indent = len(line) - len(line.lstrip())
        body = []
        for nxt in lines[i + 1 :]:
            if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= indent:
                break
            body.append(nxt[indent + 2 :] if len(nxt) > indent + 2 else "")
        return "\n".join(body) + "\n"
    raise AssertionError("job has no `run: |` block")


# --- the workflow shape ------------------------------------------------------


def test_the_heavy_compile_moved_to_its_own_macos_job():
    jobs = _jobs()
    assert list(jobs) == ["changes", "apple", "compile"], (
        "the selector, the heavy job and the stable verdict are the three jobs; "
        f"got {list(jobs)}"
    )
    apple = jobs["apple"]
    assert "runs-on: macos-latest" in apple
    assert "needs: changes" in apple
    assert "if: needs.changes.outputs.apps == 'true'" in apple, (
        "without the gate the heavy job runs on every PR again"
    )
    assert "bash macos-app/scripts/build_check.sh" in apple
    assert "bash ios-app/scripts/build_check.sh" in apple


def test_both_pr_and_push_triggers_stay():
    """A workflow that stops reporting on the PR head blocks every PR forever;
    push is the main-only backstop in pytest-legs, but here it also means every
    merge to main is compiled."""
    text = _text()
    assert "\n  push:\n" in text and "\n  pull_request:\n" in text


def test_the_selector_is_cheap_and_gets_the_pr_shas():
    changes = _jobs()["changes"]
    assert "runs-on: ubuntu-latest" in changes
    assert "fetch-depth: 0" in changes, "the touched-path diff needs both commits"
    assert "bash .github/scripts/decide_app_build.sh" in changes
    assert "BASE_SHA: ${{ github.event.pull_request.base.sha }}" in changes
    assert "PR_HEAD_SHA: ${{ github.event.pull_request.head.sha }}" in changes
    assert "build_check.sh" not in changes


def test_the_stable_check_name_reports_on_every_pr():
    compile_job = _jobs()["compile"]
    assert re.search(r"^  compile:\n    name: compile$", _text(), re.MULTILINE)
    assert "if: always()" in compile_job, (
        "a skipped `apple` job would otherwise skip the required check instead "
        "of reporting it"
    )
    assert "needs: [changes, apple]" in compile_job
    assert "runs-on: ubuntu-latest" in compile_job
    assert "build_check.sh" not in compile_job, (
        "the required check must not become a second expensive compile"
    )


@pytest.mark.parametrize(
    ("changes", "apps", "apple", "ok"),
    [
        ("success", "true", "success", True),   # the real compile passed
        ("success", "false", "skipped", True),  # Python-only PR, legitimate skip
        ("success", "true", "failure", False),  # required and red
        ("success", "true", "skipped", False),  # required but never ran
        ("success", "true", "cancelled", False),
        ("success", "false", "failure", False),  # selector said skip, job did not
        ("failure", "", "skipped", False),       # selector broke: no verdict
        ("success", "", "skipped", False),       # no apps output at all
    ],
)
def test_the_compile_verdict_cannot_report_green_without_evidence(changes, apps, apple, ok):
    script = _run_block(_jobs()["compile"])
    env = {
        "PATH": os.environ["PATH"],
        "CHANGES_RESULT": changes,
        "APPS": apps,
        "APPLE_RESULT": apple,
    }
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, check=False)
    assert (proc.returncode == 0) is ok, (
        f"changes={changes} apps={apps!r} apple={apple}: expected "
        f"{'green' if ok else 'red'}, got rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    )


# --- the selector, executed --------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def _repo_with_a_pr(tmp_path: Path, pr_files, main_files=()) -> tuple[Path, str, str]:
    """main: A -> M; pr branches at A and adds `pr_files`; returns (repo, M, head)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "ci@example.invalid")
    _git(repo, "config", "user.name", "ci")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "pr")
    for name in pr_files:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("// pr\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "pr")
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    for name in main_files:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("// main\n", encoding="utf-8")
    if main_files:
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "main moves")
    return repo, _git(repo, "rev-parse", "HEAD"), head


def _run_decide(tmp_path: Path, **env: str) -> tuple[subprocess.CompletedProcess, dict[str, str]]:
    out = tmp_path / "github_output"
    base = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_OUTPUT": str(out),
    }
    base.update(env)
    proc = subprocess.run(
        ["bash", str(DECIDE)], capture_output=True, text=True, check=False, env=base
    )
    fields: dict[str, str] = {}
    if out.exists():
        for line in out.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            fields[key] = value
    return proc, fields


def test_a_push_always_compiles():
    """The backstop: a wrong touched-path decision reddens main within minutes
    of merge instead of hiding behind a green PR."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        proc, fields = _run_decide(Path(tmp), EVENT="push")
    assert proc.returncode == 0, proc.stderr
    assert fields["apps"] == "true"


@pytest.mark.parametrize(
    ("pr_files", "main_files", "expected"),
    [
        (["hscc-api/routes.py"], (), "false"),                       # Python-only
        (["docs/readme.md"], (), "false"),
        (["macos-app/Sources/App.swift"], (), "true"),
        (["ios-app/Sources/HSCC/App.swift"], (), "true"),
        (["ios-app/scripts/widget_registry.py"], (), "true"),
        ([".github/workflows/app-build-checks.yml"], (), "true"),
        (["scripts/release_apps.sh"], (), "true"),                   # read by carplay_check.sh
        (["hscc-api/routes.py"], ["ios-app/Sources/New.swift"], "false"),  # three dots
    ],
)
def test_the_selector_runs_on_the_pr_diff(tmp_path, pr_files, main_files, expected):
    repo, base, head = _repo_with_a_pr(tmp_path, pr_files, main_files)
    proc, fields = _run_decide(
        tmp_path, EVENT="pull_request", BASE_SHA=base, PR_HEAD_SHA=head, DECIDE_REPO_ROOT=str(repo)
    )
    assert proc.returncode == 0, proc.stderr
    assert fields["apps"] == expected, proc.stdout


def test_an_unreadable_base_fails_closed(tmp_path):
    """Not knowing what changed must compile, never skip a build that might
    have been required."""
    repo, _base, head = _repo_with_a_pr(tmp_path, ["hscc-api/routes.py"])
    for env in (
        {},
        {"BASE_SHA": "deadbeef" * 5, "PR_HEAD_SHA": head},
        {"BASE_SHA": "0" * 40, "PR_HEAD_SHA": head},
    ):
        evidence = dict(env, EVENT="pull_request", DECIDE_REPO_ROOT=str(repo))
        proc, fields = _run_decide(tmp_path, **evidence)
        assert proc.returncode == 0, proc.stderr
        assert fields["apps"] == "true", evidence
