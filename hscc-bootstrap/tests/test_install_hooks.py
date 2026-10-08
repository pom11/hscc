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


def _make_repo(tmp_path, *, with_hook=True, with_guard=True):
    """Throwaway repo. Both halves must be present for install_hooks to arm it."""
    repo = tmp_path / "checkout"
    (repo / ".githooks").mkdir(parents=True)
    (repo / "scripts").mkdir()
    hook = repo / ".githooks" / "pre-commit"
    hook.write_bytes(HOOK_BODY)
    os.chmod(hook, 0o755 if with_hook else 0o644)
    if not with_hook:
        hook.unlink()
    guard = repo / "scripts" / "address_guard.py"
    guard.write_text("# stub detector\n", encoding="utf-8")
    if not with_guard:
        guard.unlink()
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
    assert "missing in this checkout" in res["reason"]
    assert install_hooks.current_hooks_path(repo) is None


def test_installer_refuses_to_arm_when_the_detector_is_absent(tmp_path):
    """The operator-flagged shape: hook present, scripts/address_guard.py not.

    The hook FAILS CLOSED without the detector, so arming here would either block
    every commit on that checkout or (worse, on an older hook revision) advertise
    protection that is not there. Arm only when both halves are present HERE.
    """
    repo = _make_repo(tmp_path, with_guard=False)   # hook present, detector absent
    res = install_hooks.install_hooks(repo)
    assert res["action"] == "skipped", res
    assert res["guard_present"] is False
    assert install_hooks.current_hooks_path(repo) is None, "must NOT arm a half-present checkout"


def test_arming_from_one_worktree_does_not_advertise_protection_elsewhere(tmp_path):
    """The measured t_ec2c2f95 review finding, encoded as a test.

    core.hooksPath is relative and lives in the COMMON config, so a value written
    from worktree A makes worktree B *report itself armed* while B resolves no
    hook and commits normally. install_hooks must not write that value from a
    checkout that has the files unless the checkout is complete, and posture()
    must call B's state what it is: armed-but-absent (fail-open).
    """
    repo, _ = _repo_with_real_guard(tmp_path, "x")
    wt = tmp_path / "outside" / "wt-premerge"
    assert _git(repo, "worktree", "add", "-q", "-b", "premerge", str(wt)).returncode == 0
    # Simulate a worktree on a PRE-merge revision: strip the guard files from it.
    shutil.rmtree(wt / ".githooks")
    (wt / "scripts" / "address_guard.py").unlink()

    assert install_hooks.install_hooks(wt)["action"] == "skipped", \
        "a checkout without the files must not arm the shared config"

    # Arming from the complete checkout is legitimate and covers this worktree once
    # the files exist there; before that, posture() must not claim 'armed'.
    assert install_hooks.install_hooks(repo)["action"] == "installed"
    p = install_hooks.posture(wt)
    assert p["state"] == "armed-but-absent", p
    assert p["core_hooks_path"] == ".githooks", p
    assert p["hook_present"] is False and p["guard_present"] is False, p
    assert install_hooks.posture(repo)["state"] == "armed"


def test_posture_four_states_are_disjoint_and_config_decides_armedness(tmp_path):
    """Each state means exactly one thing — found wiring `hscc check --repo`
    (t_5abdb13d follow-up of this card).

    The FIRST version classified `unarmed` as `not armed and not runnable`,
    which put a FRESH CLONE (both halves present, config unset) into
    `armed-but-absent`. That is wrong in the dangerous direction: a fresh clone
    advertises nothing, so the fail-open name belongs only to the case where
    the COMMON config DOES advertise protection. `armed-but-absent` is the one
    state the operator keys cron/scripts on — every false alarm there erodes
    the real one. Only the config arms the guard, so only the config may claim
    (or deny) that it is armed.
    """
    # armed: config set + runnable here.
    armed = _make_repo(tmp_path / "armed")
    assert install_hooks.install_hooks(armed)["action"] == "installed"
    p = install_hooks.posture(armed)
    assert p["state"] == "armed" and p["core_hooks_path"] == ".githooks", p

    # unarmed: BOTH halves present but the config is unset (fresh clone). The
    # files being there is not protection — git will not run them.
    fresh = _make_repo(tmp_path / "fresh")
    p = install_hooks.posture(fresh)
    assert p["state"] == "unarmed", p
    assert p["core_hooks_path"] is None, p
    assert p["hook_present"] and p["guard_present"], p
    assert "core.hooksPath unset" in p["detail"], p

    # armed-but-absent: config advertises, checkout delivers nothing runnable
    # (the shared-config scenario the operator flagged; also the 0644 hook —
    # git silently ignores a non-executable hook, so 'present' is not 'runs').
    gap = _make_repo(tmp_path / "gap")
    os.chmod(gap / ".githooks" / "pre-commit", 0o644)
    assert _git(gap, "config", "core.hooksPath", install_hooks.HOOKS_PATH_VALUE).returncode == 0
    p = install_hooks.posture(gap)
    assert p["state"] == "armed-but-absent", p
    assert p["hook_present"] is True and p["hook_executable"] is False, p

    # not-a-repo.
    p = install_hooks.posture(tmp_path / "nope")
    assert p["state"] == "not-a-repo" and p["toplevel"] is None, p


def test_default_repo_root_is_the_work_repo():
    """`python3 hscc-bootstrap/install_hooks.py` from anywhere must mean THIS repo."""
    assert install_hooks.DEFAULT_REPO_ROOT == REPO


# ── bootstrap.sh wiring (static, same style as test_orch_all_wiring.py) ──────
# bootstrap.sh is bash; the wiring that matters is that the stage EXISTS, runs
# the installer against $REPO_ROOT, and cannot be silently dropped.

def test_bootstrap_wires_the_hooks_stage():
    src = (REPO / "hscc-bootstrap" / "bootstrap.sh").read_text(encoding="utf-8")
    assert 'install_hooks.py' in src, "bootstrap must install the committed git hooks"
    stage = src.split('hdr "Install: git hooks', 1)
    assert len(stage) == 2, "the hooks stage must have its own hdr (visible in the install log)"
    body = stage[1].split('hdr "', 1)[0]
    assert '"$BOOT_DIR/install_hooks.py" "$REPO_ROOT"' in body, \
        "the installer must run against $REPO_ROOT (the checkout), not the runtime plugin dir"
    assert "installed)" in body and "verified)" in body and "skipped)" in body, \
        "all three installer actions must be reported, never swallowed"
    # A silent stage is as bad as a missing one: the warn lines are the signal
    # that the commit-time guard is INACTIVE on this machine.
    assert "INACTIVE" in body or "not armed" in body


def test_committed_hook_is_mode_100755_in_git():
    """A hook committed as 100644 is silently ignored by git on every clone."""
    proc = subprocess.run(["git", "-C", str(REPO), "ls-files", "-s", ".githooks/pre-commit"],
                          capture_output=True, text=True)
    assert proc.stdout.startswith("100755"), (
        "the committed hook must carry the exec bit in the tree, got: " + proc.stdout.strip()
    )


# ── the point of it all: the hook actually fires, incl. in worker worktrees ──

def _repo_with_real_guard(tmp_path, leaky_line):
    """A throwaway repo carrying the REAL hook + detector, guard installed."""
    repo = _make_repo(tmp_path)
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
