"""MQ3: a worker run may force-push only its own card branch.

The real fence is the ``.githooks/pre-push`` hook (armed by
``core.hooksPath`` in every linked worktree), so these cases drive both the
pure update classifier and the actual hook script through git's stdin/stdout
contract: own branch allowed, main and other branches refused, non-worker
runs untouched.
"""
import os
import subprocess
import sys
from pathlib import Path

import worker_push_guard as guard

REPO = Path(__file__).resolve().parents[2]
HOOK = REPO / ".githooks" / "pre-push"
SCRIPT = REPO / "scripts" / "worker_push_guard.py"


def _worker_env(task="t_card", branch="wt/t_card"):
    env = {"HERMES_KANBAN_TASK": task}
    if branch is not None:
        env["HERMES_KANBAN_BRANCH"] = branch
    return env


def _update(remote_ref, local_sha="b" * 40, remote_sha="a" * 40):
    return ("refs/heads/whatever", local_sha, remote_ref, remote_sha)


# ── classifier ──────────────────────────────────────────────────────────────

def test_worker_may_force_push_its_own_card_branch():
    refusals = guard.check_updates(
        [_update("refs/heads/wt/t_card")], _worker_env(),
        ancestor=lambda remote, local: False)
    assert refusals == []


def test_worker_branch_env_overrides_the_wt_convention():
    refusals = guard.check_updates(
        [_update("refs/heads/feature/x")],
        _worker_env(task="t_card", branch="feature/x"),
        ancestor=lambda remote, local: False)
    assert refusals == []


def test_worker_without_branch_env_owns_wt_task():
    refusals = guard.check_updates(
        [_update("refs/heads/wt/t_card")],
        _worker_env(branch=None), ancestor=lambda remote, local: False)
    assert refusals == []


def test_worker_may_not_force_push_main():
    refusals = guard.check_updates(
        [_update("refs/heads/main")], _worker_env(),
        ancestor=lambda remote, local: False)
    assert len(refusals) == 1
    assert "main" in refusals[0]


def test_worker_may_not_force_push_another_branch_or_a_tag():
    for ref in ("refs/heads/wt/t_other", "refs/tags/v1"):
        refusals = guard.check_updates(
            [_update(ref)], _worker_env(),
            ancestor=lambda remote, local: False)
        assert len(refusals) == 1, ref


def test_fast_forward_and_branch_creation_are_not_force_pushes():
    refusals = guard.check_updates(
        [_update("refs/heads/main", remote_sha="a" * 40)],
        _worker_env(), ancestor=lambda remote, local: True)
    assert refusals == []
    refusals = guard.check_updates(
        [_update("refs/heads/main", remote_sha=guard.ZERO_SHA)],
        _worker_env(), ancestor=lambda remote, local: False)
    assert refusals == []


def test_non_worker_runs_are_untouched():
    refusals = guard.check_updates(
        [_update("refs/heads/main")], {},
        ancestor=lambda remote, local: False)
    assert refusals == []


# ── the real hook script over git's pre-push contract ──────────────────────

def _git(cwd, *args):
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _repo_with_diverged_tips(tmp_path):
    """``main`` and ``wt/t_card`` are siblings: neither tip is an ancestor."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "worker_push_guard.py").write_text(
        SCRIPT.read_text())
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "f.txt").write_text("base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    _git(repo, "checkout", "-b", "wt/t_card")
    (repo / "card.txt").write_text("card\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "card")
    card = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")
    (repo / "main.txt").write_text("main\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "main")
    main = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "wt/t_card")
    return repo, card, main


def _run_hook(repo, stdin, env):
    full_env = dict(os.environ)
    full_env.pop("HERMES_KANBAN_TASK", None)
    full_env.pop("HERMES_KANBAN_BRANCH", None)
    full_env.update(env)
    return subprocess.run([str(HOOK), "origin", "https://example.invalid/r.git"],
                          cwd=str(repo), input=stdin, capture_output=True,
                          text=True, env=full_env)


def test_hook_allows_the_workers_own_branch(tmp_path):
    repo, card, main = _repo_with_diverged_tips(tmp_path)
    stdin = f"refs/heads/wt/t_card {card} refs/heads/wt/t_card {main}\n"
    proc = _run_hook(repo, stdin, _worker_env())
    assert proc.returncode == 0, proc.stderr


def test_hook_refuses_main_and_another_branch(tmp_path):
    repo, card, main = _repo_with_diverged_tips(tmp_path)
    for target in ("main", "wt/t_other"):
        stdin = f"refs/heads/wt/t_card {card} refs/heads/{target} {main}\n"
        proc = _run_hook(repo, stdin, _worker_env())
        assert proc.returncode == 1, (target, proc.stderr)
        assert "REFUSED by hscc worker push guard" in proc.stderr
        assert target in proc.stderr


def test_hook_is_a_no_op_outside_a_worker_run(tmp_path):
    repo, card, main = _repo_with_diverged_tips(tmp_path)
    stdin = f"refs/heads/wt/t_card {card} refs/heads/main {main}\n"
    proc = _run_hook(repo, stdin, {})
    assert proc.returncode == 0, proc.stderr


def test_hook_script_is_executable_and_points_at_the_single_detector():
    assert os.access(HOOK, os.X_OK)
    text = HOOK.read_text()
    assert "scripts/worker_push_guard.py" in text
