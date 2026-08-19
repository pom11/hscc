"""deployment.py — GENERIC "is what I merged actually live?" support.

Encodes the principle that **merged is not live**: a repo's source may have
advanced while the deployed/installed artifact still runs older code. This
cost the fleet hours repeatedly (an orphaned daemon on days-old code, a CLI
running from an install path, a proxy holding stale config, templates needing
a payload install).

Flightdeck does NOT know HOW any project deploys. The registry may declare,
per project, an opaque ``installed_version_cmd`` (a shell command printing the
deployed/installed version) and a ``version_file`` (default ``VERSION``).
Flightdeck runs the command, reads the version file, and compares the two
strings. It never parses, probes, or interprets what a project deploys.

THREE STATES, NEVER TWO: OK / DRIFTED / UNKNOWN. A project with no command,
or whose command fails, or whose version file is missing, is UNKNOWN —
explicitly distinct from OK. Reporting "OK" for something you could not check
is the exact failure mode this tool exists to prevent.

Every external call goes through an injectable ``_run`` (and ``_now`` for the
clock) so tests never touch git, the network, or any live system.
"""

from __future__ import annotations

import os
import subprocess
import time
from typing import Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .registry import Project

# Drift states -- THREE, NEVER TWO. UNKNOWN is a real state, never folded into
# OK (or anything else). "OK" may only ever be reported for a check we ran.
OK = "OK"
DRIFTED = "DRIFTED"
UNKNOWN = "UNKNOWN"

_VERSION_FILE_DEFAULT = "VERSION"

_TYPE_CMD = str  # a shell command string


def _default_run(cmd, cwd):
    """Production runner for a shell command. ``_run=None`` falls back to this.

    ``cmd`` is a shell command string and ``cwd`` the directory to run it in.
    Returns a process-like object with ``.returncode``, ``.stdout`` and
    ``.stderr`` (all str). Any OSError (missing shell, bad cwd) returns a
    synthetic failed process (returncode 128) so callers degrade gracefully
    instead of raising.
    """
    try:
        return subprocess.run(
            cmd,
            cwd=cwd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        return subprocess.CompletedProcess(
            cmd, returncode=128, stdout="", stderr=str(exc)
        )


def _dispatch(cmd, cwd, runner):
    """Resolve the injectable runner, defaulting to the real subprocess."""
    if runner is not None:
        return runner(cmd, cwd)
    return _default_run(cmd, cwd)


def _read_version_file(project) -> str | None:
    """Content of the project's version file (stripped), or None if unreadable.

    The version file defaults to ``VERSION`` at the repo root. A missing file,
    an unreadable file, or an empty file all yield None -> UNKNOWN: we refuse
    to compare against a version we could not actually read.
    """
    filename = project.version_file or _VERSION_FILE_DEFAULT
    path = os.path.join(project.repo, filename)
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read().strip()
    except (OSError, IOError, UnicodeDecodeError):
        return None
    return text or None  # empty file == no version we can trust


def version_drift(project, _run: Optional[Callable] = None):
    """Compare the repo's declared version against the installed version.

    Runs ``project.installed_version_cmd`` (an opaque shell command printing
    the deployed version) and reads ``project.version_file`` (default
    ``VERSION``) from the repo root. Returns::

        (repo_version, installed_version, state)

    where ``state`` is one of OK / DRIFTED / UNKNOWN.

    A project with no ``installed_version_cmd``, a command that exits non-zero,
    or a missing/unreadable version file is UNKNOWN — we never report OK for a
    check we could not perform.
    """
    installed_cmd = project.installed_version_cmd
    if not installed_cmd:
        # No way to know what's live -> UNKNOWN, explicitly not OK.
        return (None, None, UNKNOWN)

    cp = _dispatch(installed_cmd, project.repo, _run)
    if cp.returncode != 0:
        return (None, None, UNKNOWN)

    installed = (cp.stdout or "").strip()
    if not installed:
        # Command succeeded but printed nothing to compare -> cannot verify.
        return (None, None, UNKNOWN)

    repo_version = _read_version_file(project)
    if repo_version is None:
        return (None, installed, UNKNOWN)

    state = OK if repo_version == installed else DRIFTED
    return (repo_version, installed, state)


def last_deploy_age(
    project,
    _run: Optional[Callable] = None,
    _now: Optional[Callable] = None,
):
    """Age in seconds since the project was last deployed, or None if unknown.

    Runs ``project.deployed_at_cmd`` (an opaque shell command printing the unix
    timestamp of the last deploy). Returns the age in whole seconds, clamped to
    non-negative. Returns None (UNKNOWN) — never 0 — when there is no command,
    the command fails, or it produces no parseable timestamp. A None result
    must be surfaced as "unknown", never silently treated as "deployed just
    now".
    """
    cmd = project.deployed_at_cmd
    if not cmd:
        return None

    cp = _dispatch(cmd, project.repo, _run)
    if cp.returncode != 0:
        return None

    raw = (cp.stdout or "").strip()
    try:
        ts = float(raw)
    except ValueError:
        return None

    now = _now() if _now is not None else time.time()
    return max(0, int(now) - int(ts))
