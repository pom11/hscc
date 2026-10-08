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
target, and only fixes the exec bit when it is missing. It arms a checkout ONLY
when both halves — ``.githooks/pre-commit`` and ``scripts/address_guard.py`` —
are present *in that checkout*: the config is shared across worktrees, so arming
it where the files are absent would advertise protection that does not exist
(measured by the operator during this card's review — see ``posture()``).

``posture()`` / ``install_hooks.py --check`` is the per-checkout status readout
(``armed`` / ``unarmed`` / ``armed-but-absent`` / ``not-a-repo``), because the
config value alone cannot tell you whether the hook git will run here actually
exists and is executable.

Scope note: ``core.hooksPath`` is written to the repo's COMMON config, so every
linked worktree of a checkout that HAS the files (including the kanban
dispatcher's ``.worktrees/`` checkouts) is armed by one install. The plugin tree
copied into ``~/.hermes/plugins`` is NOT a git checkout, so an install pointed
there reports ``skipped`` rather than failing — the guard belongs to the
repository.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

HOOKS_DIR_NAME = ".githooks"
HOOKS_PATH_VALUE = HOOKS_DIR_NAME  # relative -> resolved per worktree by git
PRE_COMMIT = "pre-commit"
# The hook is plumbing; it is USELESS without this script, and it fails closed
# when the script is missing. Both must be present in the checkout being armed.
GUARD_REL = "scripts/address_guard.py"
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

    # Arm only when BOTH halves exist in THIS checkout. The hook is plumbing and
    # fails closed without the detector, so arming a path whose hook is absent (or
    # whose guard script is absent) would advertise protection that is not there.
    # Measured consequence of getting this wrong (operator, t_ec2c2f95 review):
    # core.hooksPath is relative and lives in the COMMON config, so arming it from
    # one checkout makes every worktree *report itself armed* while the ones that
    # lack the files silently find no hook and commit normally — fail-open exactly
    # where it matters. See posture() for the honest per-checkout readout.
    guard = top / GUARD_REL
    missing = [p for p, ok in ((hook, hook.is_file()), (guard, guard.is_file())) if not ok]
    if missing:
        result.update(
            action="skipped",
            guard_present=guard.is_file(),
            reason="not arming: missing in this checkout — "
            + ", ".join(str(m.relative_to(top)) for m in missing),
        )
        return result
    result["guard_present"] = True


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


def posture(repo_root=None):
    """Honest per-checkout readout of the guard — what ``hscc check --repo`` shows.

    ``core.hooksPath`` lives in the COMMON config and is RELATIVE, so the config
    value alone lies: a worktree whose checkout lacks ``.githooks/pre-commit`` (or
    ``scripts/address_guard.py``) resolves nothing and silently commits. The only
    truthful question is per-checkout: does the hook git will actually run here
    EXIST and is it EXECUTABLE. This answers that, independently of the config.

    ``state`` is one of:
      ``armed``            — config set AND the hook + detector exist and run here
      ``unarmed``          — config unset; commit-time guard not active (run bootstrap)
      ``armed-but-absent`` — config set but this checkout has no runnable hook:
                             FAIL-OPEN, and the config advertises protection. This
                             is the mismatch the operator flagged (t_ec2c2f95 review).
      ``not-a-repo``       — no git top-level here.
    """
    repo_root = Path(repo_root or DEFAULT_REPO_ROOT).expanduser().resolve()
    top = _toplevel(repo_root)
    if top is None:
        return {"state": "not-a-repo", "toplevel": None, "core_hooks_path": None,
                "hook_present": False, "hook_executable": False,
                "guard_present": False, "detail": f"no git top-level: {repo_root}"}

    hooks_path = current_hooks_path(top)
    hook = top / HOOKS_DIR_NAME / PRE_COMMIT
    guard = top / GUARD_REL
    hook_present, guard_present = hook.is_file(), guard.is_file()
    hook_executable = hook_present and bool(hook.stat().st_mode & 0o111)
    armed = hooks_path == HOOKS_PATH_VALUE
    runnable = hook_present and hook_executable and guard_present

    # The config is the ONLY thing that arms the guard, so it decides the state
    # first. (Fix found while wiring `hscc check --repo` — t_5abdb13d. The
    # original `not armed and not runnable` test put a FRESH CLONE — files
    # present, config unset, nothing advertised — into `armed-but-absent`, the
    # fail-open alarm state. Both exit non-zero so nothing caught it, but it
    # trained the operator to disbelieve exactly the state worth alarming on.)
    if not armed:
        state = "unarmed"
    elif runnable:
        state = "armed"
    else:
        state = "armed-but-absent"

    bits = []
    if not armed:
        bits.append("core.hooksPath unset")
    if not hook_present:
        bits.append(f"{HOOKS_DIR_NAME}/{PRE_COMMIT} absent")
    elif not hook_executable:
        bits.append(f"{HOOKS_DIR_NAME}/{PRE_COMMIT} not executable (git ignores it)")
    if not guard_present:
        bits.append(f"{GUARD_REL} absent")
    return {
        "state": state,
        "toplevel": str(top),
        "core_hooks_path": hooks_path,
        "hook_present": hook_present,
        "hook_executable": hook_executable,
        "guard_present": guard_present,
        "detail": "; ".join(bits) if bits else "hook present, executable, detector present",
    }


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    dry_run = "--dry-run" in argv
    check_only = "--check" in argv
    args = [a for a in argv if a not in ("--dry-run", "--check")]
    repo_root = args[0] if args else None
    if check_only:
        p = posture(repo_root)
        print(json.dumps(p))
        return 0 if p["state"] == "armed" else 1
    res = install_hooks(repo_root, dry_run=dry_run)
    print(json.dumps(res))
    return 0 if res["action"] in ("installed", "verified") else 1


if __name__ == "__main__":
    sys.exit(main())
