"""Install the committed git hooks and point ``core.hooksPath`` at them.

Why a committed hook directory instead of ``.git/hooks``: plain ``.git/hooks`` is
NOT cloned, so a hook placed there exists only on the machine that wrote it and
disappears on every fresh clone and every worker worktree. The committed
``.githooks/`` directory plus ``core.hooksPath .githooks`` is the mechanism git
itself provides for shipping hooks with a repository.

What the hook guards (``.githooks/pre-commit`` → ``scripts/address_guard.py``):
pom11/hscc is a PUBLIC repo and real operator LAN/tailnet addresses have reached
tracked files TWICE — once via an audit report (2026-08-30) and again via the
orchestrator's ledger tick on 2026-10-08. The pytest gate
(``hscc_daemon/tests/test_no_real_addresses_committed.py``) catches them, but only
when the suite runs, and a docs-only commit never runs the suite. The hook is the
commit-time trigger the guard was always missing.

Idempotent and cheap: writes ``core.hooksPath`` only when it differs from the
target, and only fixes the exec bit when it is missing.

Scope note: ``core.hooksPath`` is written to the repo's COMMON config, so every
linked worktree (including the kanban dispatcher's ``.worktrees/`` checkouts)
inherits the hook without a per-worktree step. The plugin tree copied into
``~/.hermes/plugins`` is NOT a git checkout, so an install pointed there reports
``skipped`` rather than failing — the guard belongs to the repository.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

HOOKS_DIR_NAME = ".githooks"
HOOKS_PATH_VALUE = HOOKS_DIR_NAME  # relative -> resolved per worktree by git
PRE_COMMIT = "pre-commit"
EXEC_MODE = 0o755

DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[1]


def _git(repo_root, *args, check=False):
    """Run git in ``repo_root``; return the CompletedProcess (never raises)."""
    try:
        return subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True, text=True,
        )
    except OSError as exc:  # pragma: no cover - git absent from PATH
        done = subprocess.CompletedProcess(
            ["git", *args], returncode=127, stdout="", stderr=str(exc)
        )
        return done


def _toplevel(repo_root):
    proc = _git(repo_root, "rev-parse", "--show-toplevel")
    if proc.returncode != 0:
        return None
    top = proc.stdout.strip()
    return Path(top) if top else None


def current_hooks_path(repo_root):
    """The effective ``core.hooksPath`` (None when unset)."""
    proc = _git(repo_root, "config", "--get", "core.hooksPath")
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def install_hooks(repo_root=None, *, dry_run=False):
    """Make ``<repo>/.githooks`` the hook path. Returns a summary dict.

    ``action`` is one of:
      ``installed``  — core.hooksPath was written (and/or the exec bit fixed)
      ``verified``   — already configured correctly; nothing was written
      ``skipped``    — not a git checkout, or this revision has no committed hooks
      ``error``      — a git/config write actually failed
    """
    repo_root = Path(repo_root or DEFAULT_REPO_ROOT).expanduser().resolve()
    result = {
        "action": "unknown",
        "repo_root": str(repo_root),
        "toplevel": None,
        "hooks_dir": None,
        "core_hooks_path": None,
        "hook": None,
        "mode_fixed": False,
        "reason": None,
    }

    top = _toplevel(repo_root)
    if top is None:
        # Not a git checkout (e.g. bootstrap pointed at the runtime plugin dir).
        result.update(
            action="skipped",
            reason=f"not a git checkout: {repo_root} has no git top-level",
        )
        return result

    hooks_dir = top / HOOKS_DIR_NAME
    hook = hooks_dir / PRE_COMMIT
    result.update(toplevel=str(top), hooks_dir=str(hooks_dir), hook=str(hook))

    if not hook.is_file():
        # Either an old revision that predates the committed hooks, or the hooks
        # were removed. Nothing to point at — say so instead of configuring a
        # path whose hook does not exist (git would then silently find nothing).
        result.update(
            action="skipped",
            reason=f"no committed hook at {HOOKS_DIR_NAME}/{PRE_COMMIT} in {top}",
        )
        return result

    # The exec bit survives `git checkout` only because the blob is mode 100755;
    # a file copied into place by another tool may land non-executable, and git
    # then refuses to run it (or, worse, warns and continues).
    mode = hook.stat().st_mode
    if not (mode & 0o111):
        if not dry_run:
            os.chmod(hook, EXEC_MODE)
        result["mode_fixed"] = True

    before = current_hooks_path(top)
    result["core_hooks_path"] = before
    if before == HOOKS_PATH_VALUE and not result["mode_fixed"]:
        result.update(action="verified", reason="core.hooksPath already set, hook executable")
        return result

    if dry_run:
        result.update(
            action="installed",
            reason="dry-run: would set core.hooksPath="
            f"{HOOKS_PATH_VALUE}" + (" and fix the hook exec bit" if result["mode_fixed"] else ""),
        )
        return result

    proc = _git(top, "config", "core.hooksPath", HOOKS_PATH_VALUE)
    if proc.returncode != 0:
        result.update(
            action="error",
            reason="git config core.hooksPath failed: "
            + (proc.stderr.strip() or f"exit {proc.returncode}"),
        )
        return result

    after = current_hooks_path(top)
    if after != HOOKS_PATH_VALUE:  # pragma: no cover - defensive
        result.update(action="error", reason=f"core.hooksPath reads {after!r}, not {HOOKS_PATH_VALUE!r}")
        return result

    result["core_hooks_path"] = after
    result.update(
        action="installed",
        reason="core.hooksPath set"
        + (" (exec bit repaired)" if result["mode_fixed"] else ""),
    )
    return result


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    dry_run = "--dry-run" in argv
    args = [a for a in argv if a != "--dry-run"]
    repo_root = args[0] if args else None
    res = install_hooks(repo_root, dry_run=dry_run)
    print(json.dumps(res))
    return 0 if res["action"] in ("installed", "verified") else 1


if __name__ == "__main__":
    sys.exit(main())
