"""verify.py — run a project's registry `verify` command and record the result.

The registry may declare, per project, an opaque ``verify`` shell command that
proves the project works. Flightdeck runs that command in the project's repo
directory, measures how long it took, and records PASS/FAIL (with the run
timestamp) to ``~/.flightdeck/state.yaml`` so other commands (standup) can show
"last verified 3 days ago". Staleness of CONFIDENCE matters as much as staleness
of code.

THREE STATES, NEVER TWO: PASS / FAIL / NO_VERIFY.

- PASS:      the verify command exited 0. Confidence is current.
- FAIL:      the verify command exited non-zero. Something is broken.
- NO_VERIFY: the project has no verify command configured. This is a real,
  distinct state -- explicitly NOT pass, and NOT fail. "Did I test it" cannot
  be answered "yes" for a project that has no test step; it must surface as
  "no verify configured", never silently, never counted as passing.

Flightdeck does NOT know what a verify command means -- it runs the opaque
string and interprets only the exit code and elapsed time.

Every external call (the shell command) goes through an injectable ``_run``;
the wall-clock timestamp through ``_now`` and the duration clock through
``_clock``. Tests never touch git, the network, or any live system.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Optional, TYPE_CHECKING

import yaml

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .registry import Project

PASS = "PASS"
FAIL = "FAIL"
NO_VERIFY = "NO_VERIFY"

DEFAULT_STATE = "~/.flightdeck/state.yaml"


def _resolve_state_path(path: str | None) -> str:
    """Default the state path to ~/.flightdeck/state.yaml (expanded)."""
    return os.path.expanduser(path if path is not None else DEFAULT_STATE)


def _default_run(cmd, cwd):
    """Production runner: run ``cmd`` with a shell in ``cwd``.

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


@dataclass(frozen=True)
class VerifyResult:
    """Outcome of running one project's verify command.

    ``status`` is one of PASS / FAIL / NO_VERIFY. ``duration_s`` is the wall
    time the verify command took (0.0 for NO_VERIFY). ``error`` carries the
    command's stderr (falling back to stdout) hint for a FAIL, else None.
    """

    status: str
    duration_s: float
    error: str | None = None


def run_verify(
    project,
    _run: Optional[Callable] = None,
    _clock: Optional[Callable] = None,
) -> VerifyResult:
    """Run a project's verify command and return the result.

    A project with no ``verify`` command yields NO_VERIFY immediately --
    distinct from both pass and fail, never skipped silently, never counted as
    passing. Otherwise the command runs in ``project.repo``; exit 0 is PASS,
    anything else is FAIL. Duration is measured with an injectable monotonic
    clock (default ``time.perf_counter``).
    """
    cmd = project.verify
    if not cmd:
        return VerifyResult(NO_VERIFY, 0.0)

    clock = _clock if _clock is not None else time.perf_counter
    start = clock()
    cp = _dispatch(cmd, project.repo, _run)
    duration = max(0.0, clock() - start)

    if cp.returncode == 0:
        return VerifyResult(PASS, duration)

    hint = ((cp.stderr or "") or (cp.stdout or "")).strip()
    return VerifyResult(FAIL, duration, hint or None)


# --------------------------------------------------------------------------- #
# state file — ~/.flightdeck/state.yaml
# --------------------------------------------------------------------------- #
#
# Shape:
#   verify:
#     <project>:
#       status: PASS            # PASS | FAIL | NO_VERIFY
#       timestamp: 1760000000.0 # unix epoch, when the record was written
#       duration_s: 1.234       # wall time of the verify command (0 for NO_VERIFY)
#
# A project appears in the map iff its result has been recorded. Other sections
# may be added by later commands; load/save preserve the whole document so we
# never clobber a sibling section.


def load_state(path: str | None = None) -> dict:
    """Read the state file into a dict. A missing or unparseable file is empty.

    Returns the parsed document (a dict). A missing file, a non-mapping root,
    a yaml parse failure, or an unreadable file all yield ``{}`` — never an
    exception. Callers use ``.get`` on the ``verify`` section, so a partial or
    rearranged file degrades to "no records" rather than crashing.
    """
    p = _resolve_state_path(path)
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except (yaml.YAMLError, OSError, IOError, UnicodeDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return raw


def save_state(state: dict, path: str | None = None) -> None:
    """Persist the whole state document to the state file, creating the dir."""
    p = _resolve_state_path(path)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        yaml.safe_dump(state, fh, sort_keys=False)


def record_result(
    project,
    result: VerifyResult,
    _now: Optional[Callable] = None,
    path: str | None = None,
) -> dict:
    """Record a project's verify result + timestamp into the state file.

    Persists to disk and returns the stored record dict. The timestamp is the
    injectable ``_now`` (default wall time) so tests are deterministic. Becomes
    the whole document is read first and written back, so a pre-existing
    ``verify`` section (or any sibling section) is preserved, not overwritten.
    """
    state = load_state(path)
    verify_map = state.setdefault("verify", {})
    record = {
        "status": result.status,
        "timestamp": _now() if _now is not None else time.time(),
        "duration_s": result.duration_s,
    }
    verify_map[project] = record
    save_state(state, path)
    return record
