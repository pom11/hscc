"""Which legs a workflow run selects — executed, not read (t_23bf61f3).

decide_legs.sh is where the two decisions the card owns actually live:

  * push to main runs the FULL roster (the backstop for a wrong touched-path
    map). The first draft of this gate said that in a YAML comment while the
    code diffed the push range too, so the backstop did not exist. A text pin
    would have kept passing; these cases execute the script.
  * any uncertainty (no diffable base, a failed diff) is the full roster.

The PR cases build a real throwaway git repository with a main commit AFTER the
branch point, because that is the shape where a two-dot diff silently reverses
main's newer files into the PR's changed list. THREE dots must be used; the
"branch is behind main" case below fails if that regresses.
"""

import json
import os
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
CI_SCRIPTS = HERE.parent
REPO = CI_SCRIPTS.parents[1]
DECIDE = CI_SCRIPTS / "decide_legs.sh"
RUNNER = REPO / "scripts" / "run_tests.sh"


def _roster() -> list[str]:
    """Independent parse of the runner's DIRS (the selector's parse is not reused)."""
    text = RUNNER.read_text(encoding="utf-8")
    start = text.index("DIRS=(") + len("DIRS=(")
    return text[start : text.index(")", start)].split()


def _run(tmp_path: Path, **env: str) -> tuple[subprocess.CompletedProcess, dict[str, str]]:
    out = tmp_path / "github_output"
    base = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
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


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def _repo_with_a_pr(tmp_path: Path) -> tuple[Path, str, str]:
    """main: A -> M; pr branches at A and adds one hscc-api file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "ci@example.invalid")
    _git(repo, "config", "user.name", "ci")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    a = _git(repo, "rev-parse", "HEAD")
    default = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")

    _git(repo, "checkout", "-qb", "pr")
    (repo / "hscc-api").mkdir()
    (repo / "hscc-api" / "api.py").write_text("x\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "pr change")
    head = _git(repo, "rev-parse", "HEAD")

    _git(repo, "checkout", "-q", default)
    (repo / "hscc-roles").mkdir()
    (repo / "hscc-roles" / "role.py").write_text("x\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "main moved")
    main = _git(repo, "rev-parse", "HEAD")
    assert a != main and a != head
    return repo, main, head


def _matrix_legs(fields: dict[str, str]) -> list[str]:
    return [row["leg"] for row in json.loads(fields["matrix"])]


def test_push_runs_the_full_roster(tmp_path):
    proc, fields = _run(tmp_path, EVENT="push")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert fields["legs"].split() == _roster()
    assert _matrix_legs(fields) == _roster()
    assert fields["leg_count"] == str(len(_roster()))
    assert fields["reason"].startswith("full-suite-event")


def test_a_bare_dispatch_runs_the_full_roster(tmp_path):
    proc, fields = _run(tmp_path, EVENT="workflow_dispatch")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert fields["legs"].split() == _roster()
    assert fields["reason"].startswith("full-suite-event")


def test_a_dispatch_input_is_used_verbatim_and_deduplicated(tmp_path):
    proc, fields = _run(
        tmp_path, EVENT="workflow_dispatch", INPUT_LEGS="scripts, hscc-api scripts"
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert fields["legs"] == "scripts hscc-api"
    assert _matrix_legs(fields) == ["scripts", "hscc-api"]
    assert fields["leg_count"] == "2"
    assert fields["reason"] == "dispatch-input"


def test_a_pull_request_selects_from_three_dot_diff(tmp_path):
    repo, main, head = _repo_with_a_pr(tmp_path)
    proc, fields = _run(
        tmp_path,
        EVENT="pull_request",
        BASE_SHA=main,
        PR_HEAD_SHA=head,
        DECIDE_DIFF_REPO=str(repo),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # hscc-roles was changed on MAIN after the branch point; a two-dot diff would
    # report it as touched by this PR and run its leg unnecessarily.
    assert fields["legs"] == "hscc-api", proc.stdout
    assert _matrix_legs(fields) == ["hscc-api"]
    assert fields["reason"] == "map"


def test_an_unusable_pr_base_widens_to_the_full_roster(tmp_path):
    proc, fields = _run(
        tmp_path,
        EVENT="pull_request",
        BASE_SHA="0000000000000000000000000000000000000000",
        PR_HEAD_SHA="a" * 40,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert fields["legs"].split() == _roster()
    assert fields["reason"] == "no-diffable-base"


def test_a_missing_github_output_is_a_hard_error(tmp_path):
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "EVENT": "push"}
    proc = subprocess.run(
        ["bash", str(DECIDE)], capture_output=True, text=True, check=False, env=env
    )
    assert proc.returncode != 0
    assert "GITHUB_OUTPUT" in proc.stderr
