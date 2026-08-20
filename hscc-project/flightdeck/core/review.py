"""review.py — the awaiting-review queue and the test-quality gate.

Two jobs, both behind ``flightdeck review``:

1. :func:`review_queue` — list everything GENUINELY awaiting review across
   all projects. A card only qualifies when it is review-required/blocked
   AND its branch is NOT an ancestor of ``main`` — the same strict rule that
   made standup's "NEEDS YOU" signal true (a merged branch is never awaiting
   review). Newest first.

2. :func:`check_test_quality` / :func:`run_verify_with_gate` — when the
   project's ``verify`` command runs, parse per-test timings out of the
   output and FLAG BEFORE MERGE, not after: any single test over the
   ``SLOW_TEST_SECONDS`` bar, a total suite slower than the project's
   recorded baseline, or evidence of network access in the tests.

Why the gate is its own thing: a functionally-correct change shipped this
week made a suite 33x slower (2.2s -> 72.4s) because six tests each hit a
~10s network timeout. 375 tests were green; it was only caught by reading
``--durations`` by hand. This module makes that automatic.

Every external call (running the verify command, reading/writing the
baseline file) goes through an injectable seam -- ``_run`` for the shell and
the baseline store for the file — so tests never touch git, the network, or
a live system.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

# A single test slower than this (seconds) is flagged before merge.
SLOW_TEST_SECONDS = 1.0

# Statuses that mean "the operator must review this work". Mirrors
# kanban.REVIEW_STATUSES so the queue and the classifier agree on what
# "awaiting review" means.
REVIEW_STATUSES = frozenset({"review", "blocked"})

# Textual evidence of network access in test output. Only a real signal in
# the output triggers the NETWORK flag -- we never guess network from a
# duration alone. The slow-test flag is what catches the green-with-10s-
# timeout case (each of those six tests is > 1s); this catches the case
# where the output itself names a network failure.
NETWORK_MARKERS = (
    "ConnectionRefusedError",
    "ConnectionError",
    "Connection reset",
    "socket.gaierror",
    "getaddrinfo failed",
    "[Errno 110]",
    "[Errno 111]",
    "[Errno 101]",
    "network is unreachable",
    "Network is unreachable",
    "URLError",
    "requests.exceptions",
    "TimeoutError",
    "timed out",
    "[Errno -2]",
)

# The file holding each project's recorded suite-time baseline. Same
# directory as the registry, so it travels wherever the registry does.
DEFAULT_BASELINE = "~/.flightdeck/test-baseline.yaml"


# ---------------------------------------------------------------------------
# Per-test / total parsing of pytest --durations output
# ---------------------------------------------------------------------------

# A line from `pytest --durations=N`, of the form:
#     2.34s call     tests/test_foo.py::test_bar
# The duration, the phase (call/setup/teardown), and the test node id.
_DURATION_RE = re.compile(r"^\s*([\d.]+)s\s+(\w+)\s+(\S+)\s*$")

# The pytest summary line, e.g. ``6 passed, 2 warnings in 2.34s``. With
# ``-q`` the line is bare; without it pytest wraps it in ``=== ... ===``.
# Match ``in <seconds>s`` anywhere in a line and take the LAST such match on
# the last matching line (the summary is the final line pytest prints).
_TOTAL_RE = re.compile(r"in\s+([\d.]+)s")


@dataclass
class TestTiming:
    """One parsed per-test timing row."""

    name: str
    duration: float
    phase: str = "call"


@dataclass
class VerifyResult:
    """Outcome of running a project's verify command + the quality gate."""

    project: str
    returncode: int
    total_seconds: Optional[float] = None
    tests: list[TestTiming] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    baseline_seconds: Optional[float] = None

    @property
    def ok(self) -> bool:
        """True when there is nothing to flag before merge."""
        return not self.flags


def parse_durations(output: str) -> list[TestTiming]:
    """Extract per-test timings from pytest ``--durations`` output.

    Returns one :class:`TestTiming` per parsed line, in output order. Lines
    that do not match the duration shape are ignored (they are summary or
    other progress text). A line is only a timing if it looks like one, so
    we never invent a timing from prose.
    """
    timings: list[TestTiming] = []
    for line in (output or "").splitlines():
        m = _DURATION_RE.match(line)
        if not m:
            continue
        try:
            duration = float(m.group(1))
        except ValueError:
            continue
        timings.append(TestTiming(name=m.group(3), duration=duration, phase=m.group(2)))
    return timings


def parse_total_seconds(output: str) -> Optional[float]:
    """Extract the total suite time from pytest's summary line.

    Looks for ``in <seconds>s`` at the end of a line (the pytest summary).
    Returns None when no such line is present -- we cannot report a total we
    did not see, and None is the "not measured" signal, never 0.
    """
    for line in (output or "").splitlines():
        m = _TOTAL_RE.search(line)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                return None
    return None


# ---------------------------------------------------------------------------
# Baseline store — the project's recorded suite-time total
# ---------------------------------------------------------------------------


class BaselineStore:
    """Reads and writes per-project suite-time baselines to one yaml file.

    ``path`` is the yaml file (default ~/.flightdeck/test-baseline.yaml).
    The file holds ``{projects: {<name>: {total_seconds: <float>,
    updated_at: <epoch>}}}``. A missing file is an empty store (no error).
    """

    def __init__(self, path: Optional[str] = None):
        self.path = Path(
            os.path.expanduser(path if path is not None else DEFAULT_BASELINE)
        )
        self._cache: dict[str, dict] | None = None

    def _load(self) -> dict:
        """Load the whole file (cached). A missing file is an empty mapping."""
        if self._cache is not None:
            return self._cache
        if not self.path.exists():
            self._cache = {}
            return self._cache
        try:
            import yaml

            raw = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        except Exception:
            raw = None
        data = raw if isinstance(raw, dict) else {}
        projects = data.get("projects")
        self._cache = projects if isinstance(projects, dict) else {}
        return self._cache

    def get(self, project: str) -> Optional[float]:
        """The recorded total-seconds baseline for ``project``, or None."""
        row = self._load().get(project)
        if not isinstance(row, dict):
            return None
        total = row.get("total_seconds")
        if total is None:
            return None
        try:
            return float(total)
        except (TypeError, ValueError):
            return None

    def set(self, project: str, total_seconds: float, *, _now: Optional[Callable] = None) -> None:
        """Record a new baseline total for ``project`` (persisted immediately)."""
        data = self._load()
        now = _now() if _now is not None else int(time.time())
        data[project] = {"total_seconds": float(total_seconds), "updated_at": int(now)}
        self._write(data)

    def _write(self, data: dict) -> None:
        import yaml

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            yaml.safe_dump({"projects": data}, sort_keys=False), encoding="utf-8"
        )


# ---------------------------------------------------------------------------
# The verify command runner + gate
# ---------------------------------------------------------------------------


def _default_run(cmd, cwd):
    """Production subprocess runner for a LIST command. ``_run=None`` falls
    back to this.

    Matches git_state's shape: ``(cmd_list, cwd) -> proc`` with
    ``.returncode``, ``.stdout``, ``.stderr`` (all str). Any OSError returns a
    synthetic failed process so callers degrade gracefully instead of
    raising.
    """
    try:
        return subprocess.run(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        return subprocess.CompletedProcess(
            cmd, returncode=128, stdout="", stderr=str(exc)
        )


def check_test_quality(
    output: str,
    total_seconds: Optional[float],
    *,
    baseline_seconds: Optional[float] = None,
    slow_test_seconds: float = SLOW_TEST_SECONDS,
) -> list[str]:
    """Apply the test-quality gate to parsed verify output. Returns flags.

    ``output`` is the raw verify stdout+stderr text (scanned for network
    evidence), ``total_seconds`` the parsed suite total (None if not
    measurable), ``baseline_seconds`` the project's recorded baseline (None
    on first run). Flags produced, in order:

    * ``SLOW TEST <name>: <dur>s (> <bar>s)`` — any single test over the bar.
    * ``NETWORK: <marker>`` — the output evidences network access.
    * ``SUITE SLOW: <total>s vs baseline <baseline>s`` — suite slower than
      the recorded baseline (only when a baseline exists AND a total exists).

    A clean, fast suite yields NO flags. ``baseline_seconds=`` is a *given*,
    never modified here — the caller decides when to record a new baseline.
    """
    flags: list[str] = []
    timings = parse_durations(output)

    for t in timings:
        if t.duration > slow_test_seconds:
            flags.append(
                f"SLOW TEST {t.name}: {t.duration:.2f}s (> {slow_test_seconds:g}s)"
            )

    for marker in NETWORK_MARKERS:
        if marker in (output or ""):
            flags.append(f"NETWORK: {marker} in verify output")
            break

    if (
        baseline_seconds is not None
        and total_seconds is not None
        and total_seconds > baseline_seconds
    ):
        flags.append(
            f"SUITE SLOW: {total_seconds:.2f}s vs baseline {baseline_seconds:.2f}s"
        )

    return flags


def run_verify_with_gate(
    project,
    baseline: BaselineStore,
    *,
    _run: Optional[Callable] = None,
    _now: Optional[Callable] = None,
    slow_test_seconds: float = SLOW_TEST_SECONDS,
) -> VerifyResult:
    """Run ``project.verify`` and apply the test-quality gate.

    ``baseline`` is a :class:`BaselineStore` giving/recording the project's
    suite-time baseline. ``_run`` is the injectable shell runner, ``_now`` the
    injectable clock for baseline timestamps.

    Semantics:

    * Flags fire even on a green suite — a 72s green run is exactly the case
      the gate exists to catch. ``result.ok`` is False when anything is
      flagged, so the caller can refuse to offer merge.
    * The baseline is recorded ONLY on a clean run (no flags). A flagged run
      leaves the baseline untouched, so a regression keeps flagging until a
      clean run lowers the total back toward (or under) it. This is what
      makes a self-healing baseline that still catches regressions.
    * A project with no ``verify`` command cannot be gated; the command's
      returncode is 127 (caller surfaces this) and only the ``returncode``
      field is meaningful.
    """
    cmd = project.verify
    if not cmd:
        return VerifyResult(project=project.name, returncode=127)

    # The registry stores verify as a shell string; run it through a list
    # command so the shared (git_state-style) ``_run`` seam applies uniformly.
    cp = _default_run(["sh", "-c", cmd], project.repo) if _run is None else _run(
        ["sh", "-c", cmd], project.repo
    )
    output = f"{cp.stdout or ''}\n{cp.stderr or ''}"

    total = parse_total_seconds(output)
    timings = parse_durations(output)
    baseline_seconds = baseline.get(project.name)
    flags = check_test_quality(
        output,
        total,
        baseline_seconds=baseline_seconds,
        slow_test_seconds=slow_test_seconds,
    )

    result = VerifyResult(
        project=project.name,
        returncode=cp.returncode,
        total_seconds=total,
        tests=timings,
        flags=flags,
        baseline_seconds=baseline_seconds,
    )

    # Record the baseline only when the run is clean — a flagged run must not
    # ratchet the baseline up toward the regression.
    if not flags and total is not None:
        baseline.set(project.name, total, _now=_now)

    return result


# ---------------------------------------------------------------------------
# The awaiting-review queue
# ---------------------------------------------------------------------------


def review_queue(
    cards: list[dict],
    *,
    now: Optional[int] = None,
) -> list[dict]:
    """Filter + sort cards into the awaiting-review queue.

    ``cards`` is a list of flightdeck card dicts, each ENRICHED by the caller
    with the per-card git facts the queue needs:

    * ``project`` — the registry project name (str)
    * ``id``, ``title``, ``branch``, ``status``, ``created_at``
    * ``branch_exists`` (bool), ``is_merged`` (bool)

    A card qualifies iff status is review/blocked AND its branch exists AND
    is NOT an ancestor of ``main`` (unmerged). Missing facts default to the
    conservative reading (no branch, merged==False, status=="") so a card
    never shows up in the queue on a guess.

    Returns rows ``[{project, card_id, title, branch, age_seconds}]`` sorted
    newest-first (descending ``created_at``), where ``age_seconds`` is time
    since the card was created. A card with no ``created_at`` sorts last
    (age unknown) but still appears — the queue never silently drops a row.
    """
    if now is None:
        now = int(time.time())

    rows: list[dict] = []
    for card in cards:
        status = str(card.get("status") or "")
        if status not in REVIEW_STATUSES:
            continue
        # Conservative defaults: we need an existing, unmerged branch to
        # trust that this really awaits review.
        if not card.get("branch_exists"):
            continue
        if card.get("is_merged"):
            continue

        created = card.get("created_at")
        try:
            created = int(created) if created is not None else None
        except (TypeError, ValueError):
            created = None

        rows.append(
            {
                "project": card.get("project"),
                "card_id": card.get("id"),
                "title": card.get("title"),
                "branch": card.get("branch"),
                "age_seconds": None if created is None else max(0, now - created),
                "_created": created,
            }
        )

    # Newest first by created_at; rows with unknown age sort after known ones
    # but are NOT dropped.
    rows.sort(key=lambda r: (r["_created"] is None, -(r["_created"] or 0)))
    for r in rows:
        r.pop("_created", None)
    return rows
