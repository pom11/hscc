"""Tests for install_hooks.py — the commit-time address guard's installer (t_ec2c2f95).

Why: the guard in scripts/address_guard.py was already correct but had no trigger
on docs-only commits, so real operator addresses reached the PUBLIC repo twice. The
fix is a committed ``.githooks/`` + ``core.hooksPath`` — and a committed hook dir
does nothing until the config points at it, which is what this installer does on
every bootstrap run.

Every test here works in a throwaway git repo under tmp_path; none of them touch
the real checkout's config.
"""

import importlib.util
import os
import shutil
import stat
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "install_hooks_under_test", REPO / "hscc-bootstrap" / "install_hooks.py")
assert _spec is not None and _spec.loader is not None
install_hooks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(install_hooks)

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@e.invalid", "-c", "commit.gpgsign=false"]
HOOK_BODY = b"#!/bin/sh\n# committed hook\nexit 0\n"


def _git(repo, *args):
    return subprocess.run(GIT + ["-C", str(repo), *args], capture_output=True, text=True)


def _make_repo(tmp_path, *, with_hook=True):
    repo = tmp_path / "checkout"
    (repo / ".githooks").mkdir(parents=True)
    hook = repo / ".githooks" / "pre-commit"
    hook.write_bytes(HOOK_BODY)
    os.chmod(hook, 0o755 if with_hook else 0o644)
    if not with_hook:
        hook.unlink()
    (repo / "README.md").write_text("# x\n", encoding="utf-8")
    assert _git(repo, "init", "-q", "-b", "main").returncode == 0
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


# ── the happy path: a fresh clone gets the guard ─────────────────────────────

def test_fresh_install_sets_hooks_path(tmp_path):
    repo = _make_repo(tmp_path)
    assert install_hooks.current_hooks_path(repo) is None
    res = install_hooks.install_hooks(repo)
    assert res["action"] == "installed", res
    assert install_hooks.current_hooks_path(repo) == install_hooks.HOOKS_PATH_VALUE


def test_hooks_path_value_is_relative_so_worktrees_resolve_theirs(tmp_path):
    """An ABSOLUTE hooksPath would make every worktree run the main checkout's
    hook (wrong repo, wrong staged tree). The value must be relative."""
    assert not os.path.isabs(install_hooks.HOOKS_PATH_VALUE)
    assert install_hooks.HOOKS_PATH_VALUE == ".githooks"


def test_second_run_is_a_verified_noop(tmp_path):
    """Bootstrap re-runs constantly; a second run must not churn the config."""
    repo = _make_repo(tmp_path)
    first = install_hooks.install_hooks(repo)
    mtime_before = (repo / ".git" / "config").stat().st_mtime_ns
    second = install_hooks.install_hooks(repo)
    assert first["action"] == "installed"
    assert second["action"] == "verified", second
    assert (repo / ".git" / "config").stat().st_mtime_ns == mtime_before, "verified must not write"


def test_installer_repairs_a_missing_exec_bit(tmp_path):
    """A hook copied in by another tool can land 0644; git then never runs it."""
    repo = _make_repo(tmp_path)
    hook = repo / ".githooks" / "pre-commit"
    os.chmod(hook, 0o644)
    res = install_hooks.install_hooks(repo)
    assert res["action"] == "installed" and res["mode_fixed"] is True, res
    assert hook.stat().st_mode & stat.S_IXUSR


def test_installer_fixes_a_wrong_hooks_path(tmp_path):
    repo = _make_repo(tmp_path)
    _git(repo, "config", "core.hooksPath", ".git/hooks")   # the broken default
    res = install_hooks.install_hooks(repo)
    assert res["action"] == "installed", res
    assert install_hooks.current_hooks_path(repo) == ".githooks"


def test_dry_run_writes_nothing(tmp_path):
    repo = _make_repo(tmp_path)
    res = install_hooks.install_hooks(repo, dry_run=True)
    assert res["action"] == "installed" and "dry-run" in res["reason"]
    assert install_hooks.current_hooks_path(repo) is None


# ── refusing to fake success ─────────────────────────────────────────────────

def test_not_a_git_checkout_is_skipped(tmp_path):
    plain = tmp_path / "runtime-plugin-dir"
    (plain / ".githooks").mkdir(parents=True)
    (plain / ".githooks" / "pre-commit").write_bytes(HOOK_BODY)
    res = install_hooks.install_hooks(plain)
    assert res["action"] == "skipped", res
    assert "not a git checkout" in res["reason"]
    assert install_hooks.current_hooks_path(plain) is None


def test_repo_without_a_committed_hook_is_skipped(tmp_path):
    """Configuring a path whose hook does not exist would silently guard nothing."""
    repo = _make_repo(tmp_path, with_hook=False)
    res = install_hooks.install_hooks(repo)
    assert res["action"] == "skipped", res
    assert "no committed hook" in res["reason"]
    assert install_hooks.current_hooks_path(repo) is None


def test_default_repo_root_is_the_work_repo():
    """`python3 hscc-bootstrap/install_hooks.py` from anywhere must mean THIS repo."""
    assert install_hooks.DEFAULT_REPO_ROOT == REPO


# ── the point of it all: the hook actually fires, incl. in worker worktrees ──

def _repo_with_real_guard(tmp_path, leaky_line):
    """A throwaway repo carrying the REAL hook + detector, guard installed."""
    repo = _make_repo(tmp_path)
    (repo / "scripts").mkdir()
    shutil.copy2(REPO / "scripts" / "address_guard.py", repo / "scripts" / "address_guard.py")
    shutil.copy2(REPO / ".githooks" / "pre-commit", repo / ".githooks" / "pre-commit")
    os.chmod(repo / ".githooks" / "pre-commit", 0o755)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "install guard")
    return repo, leaky_line


def test_install_then_commit_is_blocked(tmp_path):
    leak = "host " + "".join(["192", ".168.88.244"]) + "\n"
    repo, leak = _repo_with_real_guard(tmp_path, leak)
    # Not installed yet -> the leak commits (this is the gap).
    (repo / "notes.md").write_text(leak, encoding="utf-8")
    _git(repo, "add", "notes.md")
    assert _git(repo, "commit", "-q", "-m", "docs: leak").returncode == 0, \
        "without hooksPath the guard must not fire (proves the config is what arms it)"
    _git(repo, "reset", "-q", "--hard", "HEAD~1")

    assert install_hooks.install_hooks(repo)["action"] == "installed"
    (repo / "notes.md").write_text(leak, encoding="utf-8")
    _git(repo, "add", "notes.md")
    blocked = _git(repo, "commit", "-q", "-m", "docs: leak again")
    assert blocked.returncode != 0, "installed guard must block the commit"
    assert "notes.md:1" in blocked.stderr, blocked.stderr


def test_linked_worktree_inherits_the_guard(tmp_path):
    """The dispatcher's worker worktrees must be covered by ONE install.

    core.hooksPath lands in the COMMON config, so a worktree created later — even
    outside the repo directory — resolves the committed hook from its own checkout.
    """
    leak = "subnet " + "".join(["192", ".168.88.244"]) + "/24\n"
    repo, _ = _repo_with_real_guard(tmp_path, leak)
    assert install_hooks.install_hooks(repo)["action"] == "installed"

    wt = tmp_path / "outside" / "wt1"          # kanban-style: worktree OUTSIDE the repo
    add = _git(repo, "worktree", "add", "-q", "-b", "wt1", str(wt))
    assert add.returncode == 0, add.stderr
    assert wt / ".githooks" / "pre-commit"
    assert _git(wt, "config", "--get", "core.hooksPath").stdout.strip() == ".githooks"

    (wt / "docs.md").write_text(leak, encoding="utf-8")
    _git(wt, "add", "docs.md")
    blocked = _git(wt, "commit", "-q", "-m", "docs: leak in worktree")
    assert blocked.returncode != 0, "a worker worktree commit must be guarded too"
    assert "docs.md:1" in blocked.stderr, blocked.stderr
    # And a clean commit in the same worktree still works.
    _git(wt, "reset", "-q", "--hard")
    (wt / "clean.md").write_text("all good\n", encoding="utf-8")
    _git(wt, "add", "clean.md")
    assert _git(wt, "commit", "-q", "-m", "docs: clean").returncode == 0
