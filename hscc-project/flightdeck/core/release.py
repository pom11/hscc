"""release.py — the refusal + execution halves of `flightdeck release`.

The refusal half (:func:`preconditions`) implements the release preconditions
and the dry-run plan. It MUTATES NOTHING: no git writes, no pushes, no
`gh release`. The execution half (:func:`execute`) runs the release steps
(bump, commit, tag, push, gh release, install, verify installed) through the
injectable ``_run``, and an ``--apply`` caller MUST run :func:`preconditions`
first and only call ``execute`` when it returned no problems.

The refusal logic is deliberately the valuable half. Eight releases were cut
by hand recently, each ~6 steps, and twice the install step was missed so
"merged" was never "live". There is NO override flag: a release with red
tests is never the right answer. If any precondition fails, the release is
refused with a clear reason.

:func:`execute` runs all seven release steps (bump, commit, tag, push, gh
release, install, verify installed) through the same injectable ``_run``,
reporting each as it runs and stopping at the first failure. The last two
steps are the "merged is not live" guard: after releasing, the registry's
install command runs, then the INSTALLED version is re-read and compared to
the released version. A mismatch or an inability to confirm is reported as
FAILED/UNVERIFIED — never a clean success.

Preconditions are checked in a fixed order and ALL are reported (not just the
first), so the operator can fix every blocker in one pass:

1. the working tree is clean
2. the current branch is the project's main branch (``main``)
3. the project's registry ``verify`` command passes (reused from
   :mod:`flightdeck.core.verify` — never reimplemented here)
4. ``CHANGELOG.md`` has a section for the target version (the prose is never
   auto-generated; the command prints a skeleton for the operator to fill in)
5. the target version is GREATER than the current ``VERSION`` file contents

Every external call (git status, git branch, the verify command) goes through
the injectable ``_run`` using the same shell-string convention as
:mod:`flightdeck.core.verify`, and the identical ``_run`` is passed straight
through to :func:`~flightdeck.core.verify.run_verify`, so tests never touch
git, the network, or any live system.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import Callable, Optional, TYPE_CHECKING

from . import verify

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .registry import Project

# The project's main branch. The registry has no field for this, so it is a
# well-known constant. (The worked example in docs/ is on `main`.)
DEFAULT_MAIN_BRANCH = "main"

# The source-version file at the repo root (mirrors deployment.py's default).
DEFAULT_VERSION_FILE = "VERSION"

# The changelog file at the repo root.
DEFAULT_CHANGELOG = "CHANGELOG.md"

# The PEP 621 metadata file at the repo root. A project may declare its version
# there AND in the VERSION file — the v0.2.0 incident proved both must be kept
# in lockstep, or the tag/release goes out against a package that identifies
# itself as the previous version.
DEFAULT_PYPROJECT = "pyproject.toml"

# A `[project] version = "..."` line (leading indent allowed). Only a LITERAL
# version inside the [project] table counts; a dynamic version (`dynamic =
# ["version"]`) has no such line and is intentionally left alone.
_INLINE_VERSION_RE = re.compile(r'^\s*version\s*=\s*"[^"]*"\s*$')


@dataclass(frozen=True)
class Problem:
    """A single refused precondition, with a short ``code`` and human message."""

    code: str
    message: str


# --------------------------------------------------------------------------- #
# Injectable runner (shell-string convention, shared with core.verify)
# --------------------------------------------------------------------------- #

def _default_run(cmd, cwd):
    """Production runner: run ``cmd`` as a shell string in ``cwd``.

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


# --------------------------------------------------------------------------- #
# Version parsing / comparison
# --------------------------------------------------------------------------- #

def _parse_version(text: str) -> tuple[tuple[int, ...], int, str]:
    """Return a semver-aware ordering key for ``text``.

    The key is a uniform triple ``(core, release, suffix)``:

    * ``core`` — the numeric ``(major, minor, patch)`` parts (``(1, 8, 1)``)
    * ``release`` — an integer rank ordering a prerelease strictly BELOW its
      same-core GA: ``2`` for a GA (no prerelease/build suffix), ``1`` for a
      prerelease like ``rc1``/``beta2``, ``0`` when there is no usable core
    * ``suffix`` — the raw prerelease text (``"-rc1"``), used as an explicit
      tie-breaker so ``rc1`` < ``rc2``; ``""`` for a GA

    Because ``(core, 1, ...)`` < ``(core, 2, ...)``, a prerelease like
    ``1.8.1-rc1`` now compares LESS than its same-core GA ``1.8.1`` — the semver
    rule, so the ``version-not-greater`` precondition correctly blocks releasing
    an ``rc1`` after its GA has shipped. It still compares GREATER than
    ``1.8.0`` or any earlier ``1.8.x`` line, so valid pre-GA candidates are
    allowed. Core parts compare numerically (``1.8.10`` > ``1.8.9``, never a
    string sort), and a leading ``v``/``V`` is ignored. A string with no usable
    core (e.g. ``abc``) yields ``((0,), 0, "")`` which sorts below everything.
    """
    raw = text.strip()
    # numeric core: major(.minor(.patch)) after an optional v/V. The minor and
    # patch groups are optional so ``1.8`` and even just ``1`` compare sensibly.
    mc = re.match(r"^[vV]?(\d+)(?:\.(\d+))?(?:\.(\d+))?", raw)
    if not mc:
        return ((0,), 0, "")
    core = tuple(int(g) for g in mc.groups() if g is not None)

    # anything after the core is a prerelease/build suffix. Normalize the common
    # ``1.8.1rc1`` (no separator) into ``-rc1`` so it is still a prerelease.
    rest = raw[mc.end():]
    if rest and not rest.startswith(("-", "+", ".")):
        rest = "-" + rest
    if not rest:
        return (core, 2, "")
    return (core, 1, rest)



# --------------------------------------------------------------------------- #
# CHANGELOG detection + skeleton
# --------------------------------------------------------------------------- #

def _heading_tokens(line: str) -> list[str]:
    """Split a markdown heading line into its first-token-friendly parts.

    Strips leading ``#`` markers, an enclosing ``[...]`` wrapper (e.g.
    ``[1.8.1]``), and surrounding whitespace, then whitespace-splits:
    ``## [1.8.1] — title`` -> ``['1.8.1', '—', 'title']``. Returns [] for a
    non-heading or empty body.
    """
    body = line.lstrip().lstrip("#").strip()
    if not body:
        return []
    if body.startswith("["):
        end = body.find("]")
        if end != -1:
            body = body[1:end]
    return body.split()


def _has_changelog_section(changelog_text: str, version: str) -> bool:
    """True if ``changelog_text`` has a heading for exactly ``version``.

    Matches the version whether or not it carries a ``v`` prefix and regardless
    of a trailing title in the heading. ``## [1.8.1] — …``, ``## 1.8.1`` and
    ``## v1.8.1`` all match ``1.8.1``. A missing CHANGELOG is handled by the
    caller (the read yields no text -> no section).
    """
    core = version.lstrip("v")
    variants = {core, "v" + core}
    for line in changelog_text.splitlines():
        if not line.lstrip().startswith("#"):
            continue
        tokens = _heading_tokens(line)
        if tokens and tokens[0] in variants:
            return True
    return False


def _changelog_section(changelog_text: str, version: str) -> str | None:
    """Extract the body of the changelog section for exactly ``version``.

    Returns the section text — from the section's heading line up to (but not
    including) the next heading of the SAME level or higher, which is what the
    annotated tag message and the GitHub release notes carry. Deeper sub-headings
    (``### Added`` etc.) belong to the section and are kept. Handles the same
    heading shapes as :func:`_has_changelog_section` (``## [1.9.0] — title``,
    ``## 1.9.0``, ``## v1.9.0``). Returns None if the section is not present.
    """
    core = version.lstrip("v")
    variants = {core, "v" + core}
    lines = changelog_text.splitlines()
    start = None
    section_level = None
    for i, line in enumerate(lines):
        if not line.lstrip().startswith("#"):
            continue
        stripped = line.lstrip()
        level = len(stripped) - len(stripped.lstrip("#"))
        if start is None:
            tokens = _heading_tokens(line)
            if tokens and tokens[0] in variants:
                start = i
                section_level = level
            continue
        # a heading at the same level or higher ends the section
        if level <= section_level:
            return "\n".join(lines[start:i])
    if start is not None:
        return "\n".join(lines[start:])
    return None


def changelog_skeleton(version: str) -> str:
    """The markdown section skeleton for ``version``, for the operator to fill.

    The prose is NEVER auto-generated — the operator writes what changed. This
    is a shape to fill, not a summary of the work.
    """
    return (
        f"## [{version.lstrip('v')}] — describe this release\n"
        "\n"
        "### Added\n"
        "\n"
        "### Changed\n"
        "\n"
        "### Fixed\n"
    )


# --------------------------------------------------------------------------- #
# file reads (stdlib only, never via _run)
# --------------------------------------------------------------------------- #

def _read_repo_file(project, filename: str) -> str | None:
    """Read a file from the repo root, or None if missing/unreadable."""
    import os

    path = os.path.join(project.repo, filename)
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except (OSError, IOError, UnicodeDecodeError):
        return None


def _read_version(project) -> str | None:
    """Current version in the project's ``VERSION`` file (stripped), or None.

    None means the version could not be read — we refuse to compare against a
    version we could not actually see, mirroring deployment.py's stance.
    """
    raw = _read_repo_file(project, project.version_file or DEFAULT_VERSION_FILE)
    if raw is None:
        return None
    text = raw.strip()
    return text or None


def _parse_pyproject_version(text: str) -> str | None:
    """The literal ``[project] version = \"...\"`` value in pyproject.toml.

    None means the file (or the ``[project]`` table) declares no literal
    version — including a dynamic version (``dynamic = [\"version\"]``).
    """
    in_project = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            # a table header — track whether we are inside the [project] table
            in_project = stripped == "[project]"
            continue
        if in_project and _INLINE_VERSION_RE.match(line):
            return line.strip().split("=", 1)[1].strip().strip('"')
    return None


def _read_pyproject_version(project) -> str | None:
    """The version declared by ``[project] version`` in pyproject.toml, or None.

    None covers every \"no literal version declared\" case: no pyproject.toml at
    the repo root, a pyproject with no ``[project] version`` field, and a
    dynamic version (``dynamic = [\"version\"]``). Only a literal
    ``version = \"X.Y.Z\"`` inside the ``[project]`` table counts as a declared
    source — and only that case must be kept in lockstep with the VERSION file.
    """
    text = _read_repo_file(project, DEFAULT_PYPROJECT)
    if text is None:
        return None
    return _parse_pyproject_version(text)


def _replace_pyproject_version(text: str, version: str) -> tuple[str, bool]:
    """Rewrite only the ``[project] version = \"...\"`` value in a pyproject.toml.

    Returns ``(new_text, updated)``. Only the value inside that single line is
    changed; every other byte — indentation, the key spelling, trailing newline,
    surrounding tables and comments — is preserved exactly (byte-identical
    outside the version line's value). A leading ``v`` on the version is
    stripped. ``updated`` is False when the file declares no literal
    ``[project] version`` (absent table or dynamic version), and the text is
    returned unchanged.
    """
    version = version.lstrip("v")
    lines = text.splitlines(keepends=True)
    out = []
    updated = False
    in_project = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            in_project = stripped == "[project]"
            out.append(line)
            continue
        if in_project and _INLINE_VERSION_RE.match(line):
            body = line.rstrip("\r\n")
            ending = line[len(body):] or "\n"
            indent = body[: len(body) - len(body.lstrip())]
            out.append(f'{indent}version = "{version}"{ending}')
            updated = True
            # a well-formed pyproject has one [project] block; stop scoping after
            in_project = False
            continue
        out.append(line)
    return "".join(out), updated


def _write_pyproject_version(project, version: str) -> bool:
    """Update ``[project] version`` in pyproject.toml in place.

    Returns True only when the file was actually rewritten. False means there
    was nothing to update (no pyproject.toml, no literal ``[project] version``,
    or a dynamic version) — which is fine, the caller bumps what exists.
    Direct in-process file I/O, like every other repo-file read/write in this
    module; it never goes through ``_run`` because it must rewrite exactly one
    field and touch no other byte.
    """
    import os

    text = _read_repo_file(project, DEFAULT_PYPROJECT)
    if text is None:
        return False
    new_text, updated = _replace_pyproject_version(text, version)
    if not updated:
        return False
    path = os.path.join(project.repo, DEFAULT_PYPROJECT)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new_text)
    return True


# --------------------------------------------------------------------------- #
# preconditions
# --------------------------------------------------------------------------- #

def preconditions(
    project,
    version: str,
    *,
    _run: Optional[Callable] = None,
    main_branch: str = DEFAULT_MAIN_BRANCH,
) -> list[Problem]:
    """Return every failed precondition in check order (empty = all clear).

    Mutates nothing. Each check is independent and all problems are returned,
    so the caller can report every blocker at once. ``main_branch`` defaults to
    ``main`` and is injectable for tests.
    """
    problems: list[Problem] = []

    # 1. working tree clean
    cp = _dispatch("git status --porcelain", project.repo, _run)
    if cp.returncode != 0:
        problems.append(
            Problem("git-unavailable", f"could not check git status in {project.repo}")
        )
    else:
        changed = [ln for ln in cp.stdout.splitlines() if ln.strip()]
        if changed:
            problems.append(
                Problem("dirty", f"working tree is dirty in {project.repo}")
            )

    # 2. current branch is the project's main branch
    cp = _dispatch("git rev-parse --abbrev-ref HEAD", project.repo, _run)
    if cp.returncode != 0:
        problems.append(
            Problem(
                "branch-unavailable",
                f"could not determine the current branch in {project.repo}",
            )
        )
    else:
        branch = cp.stdout.strip()
        if branch != main_branch:
            problems.append(
                Problem(
                    "wrong-branch",
                    f"on branch {branch!r}, expected {main_branch!r} — "
                    f"refusing to release from a non-main branch",
                )
            )

    # 3. the project's registry verify command passes (reused from core.verify)
    result = verify.run_verify(project, _run=_run)
    if result.status == verify.FAIL:
        hint = (result.error or "").strip()
        msg = "verify FAILED"
        if hint:
            msg += f": {hint}"
        problems.append(Problem("verify-failed", msg))
    elif result.status == verify.NO_VERIFY:
        # Not a FAIL, but a release we cannot confirm is green is never safe.
        problems.append(
            Problem(
                "no-verify",
                "project has no verify command configured — cannot confirm the "
                "release is green, refusing",
            )
        )

    # 4. CHANGELOG.md has a section for this version
    changelog = _read_repo_file(project, DEFAULT_CHANGELOG)
    if changelog is None:
        problems.append(
            Problem(
                "missing-changelog",
                f"no {DEFAULT_CHANGELOG} in {project.repo} — add a section for "
                f"{version} before releasing",
            )
        )
    elif not _has_changelog_section(changelog, version):
        problems.append(
            Problem(
                "missing-changelog",
                f"{DEFAULT_CHANGELOG} has no section for {version} — write it "
                f"before releasing",
            )
        )

    # 5. version is GREATER than the current VERSION file contents
    current = _read_version(project)
    if current is None:
        problems.append(
            Problem(
                "version-unreadable",
                f"could not read the current version file "
                f"({project.version_file or DEFAULT_VERSION_FILE}) in "
                f"{project.repo}",
            )
        )
    elif _parse_version(version) <= _parse_version(current):
        problems.append(
            Problem(
                "version-not-greater",
                f"version {version} is not greater than the current {current} — "
                f"no re-releasing or going backwards",
            )
        )

    # 6. the declared version sources agree with each other. VERSION and
    #    pyproject's `[project] version` must be in lockstep BEFORE the bump —
    #    releasing from an inconsistent state is exactly how the v0.2.0 tag went
    #    out against a package that still identified itself as 0.1.0. Refuse with
    #    BOTH values so the operator fixes the drift in one pass. Skips / is fine
    #    when either source is absent or dynamic (nothing to compare).
    pyproj = _read_pyproject_version(project)
    if current is not None and pyproj is not None and pyproj != current:
        problems.append(
            Problem(
                "version-mismatch",
                f"version sources disagree — "
                f"{project.version_file or DEFAULT_VERSION_FILE} says {current!r} "
                f"but {DEFAULT_PYPROJECT} says {pyproj!r}; releasing from an "
                f"inconsistent state is unsafe — align them first",
            )
        )

    return problems


# --------------------------------------------------------------------------- #
# install + post-install verification — the "merged is not live" guard
# --------------------------------------------------------------------------- #

# Post-install verification status. THREE states, never two: a release we ran
# install for and confirmed matches the released version is VERIFIED; one we
# could not confirm is UNVERIFIED; one where install failed or the installed
# version DIFFERS from the released version is FAILED. UNVERIFIED is a real,
# distinct state — never folded into VERIFIED. "The release verified" may only
# be claimed for a check we actually performed.
VERIFIED = "VERIFIED"
UNVERIFIED = "UNVERIFIED"
FAILED = "FAILED"


@dataclass(frozen=True)
class InstallOutcome:
    """Outcome of the install + post-install verification phase.

    ``status`` is one of VERIFIED / UNVERIFIED / FAILED. ``released_version``
    is the version that was released (leading ``v`` stripped). ``installed_version``
    is what ``installed_version_cmd`` reported (None when it could not be read —
    including when no install command means we never reach it). ``install_ran``
    records whether an install command was actually executed, so the command
    layer can distinguish "no install command declared" from "installed but
    could not verify". ``detail`` carries a human-readable reason for anything
    short of a clean VERIFIED.
    """

    status: str
    released_version: str
    installed_version: str | None
    install_ran: bool
    detail: str = ""


@dataclass(frozen=True)
class ReleaseResult:
    """Result of an executed release: completed steps + post-install outcome.

    ``completed`` lists the step names that ran, in order (bump, commit, tag,
    push, gh-release, and — when an install command is declared — install and
    verify). ``outcome`` is the :class:`InstallOutcome` for the install +
    verification phase, so the command layer knows the release was only a clean
    success when ``outcome.status`` is :data:`VERIFIED`.

    ``files_written`` lists the repo-root files the bump step actually wrote
    (e.g. ``[\"VERSION\", \"pyproject.toml\"]``) so the command layer can report
    exactly which version sources were updated — the bump keeps every declared
    source in lockstep.

    ``verified`` is a convenience: True only when the released version was
    actually confirmed live.
    """

    completed: list[str]
    outcome: InstallOutcome
    files_written: tuple[str, ...] = ()

    @property
    def verified(self) -> bool:
        return self.outcome.status == VERIFIED


def _read_installed_version(project, _run: Optional[Callable] = None) -> str | None:
    """Run ``installed_version_cmd`` and return its (stripped) output, or None.

    None covers every unverifiable case: no command declared, a command that
    exits non-zero, or a command that prints nothing. We never return an empty
    string as a version — an absent version is None, so the caller treats it as
    UNVERIFIED, never as a match.
    """
    cmd = project.installed_version_cmd
    if not cmd:
        return None
    cp = _dispatch(cmd, project.repo, _run)
    if cp.returncode != 0:
        return None
    installed = (cp.stdout or "").strip()
    return installed or None


def verify_after_install(
    project,
    released_version: str,
    *,
    install_ran: bool,
    _run: Optional[Callable] = None,
) -> InstallOutcome:
    """Build the install + post-install verification outcome.

    This is the "merged is not live" guard: after installing, the installed
    artifact must actually carry the released version. A mismatch is a
    FAILURE, never a warning. ``install_ran`` records whether an install
    command was actually executed (its dispatch is the caller's job).

    If the registry declares no ``install_cmd`` (and so install never ran), we
    CANNOT verify, so the result is UNVERIFIED, never VERIFIED. A release
    whose install could not be performed must not read as a clean success.

    Status mapping:

    * no ``install_cmd`` declared (install never ran) -> UNVERIFIED
      (install_ran=False)
    * installed version cannot be re-read (no ``installed_version_cmd``, or it
      fails / prints nothing)           -> UNVERIFIED (install_ran passed through)
    * installed version != released      -> FAILED (merged is not live)
    * installed version == released      -> VERIFIED
    """
    released = released_version.lstrip("v")

    if not project.install_cmd:
        return InstallOutcome(
            UNVERIFIED,
            released,
            None,
            install_ran=False,
            detail="no install_cmd declared in the registry — install skipped, "
            "installed version cannot be verified",
        )

    installed = _read_installed_version(project, _run)
    if installed is None:
        return InstallOutcome(
            UNVERIFIED,
            released,
            None,
            install_ran=install_ran,
            detail="installed but could not re-read the installed version — "
            "merged is not confirmed live",
        )

    if installed == released:
        return InstallOutcome(
            VERIFIED, released, installed, install_ran=install_ran
        )

    return InstallOutcome(
        FAILED,
        released,
        installed,
        install_ran=install_ran,
        detail="installed version differs from the released version — "
        "merged is not live",
    )


# --------------------------------------------------------------------------- #
# execute — the execution half (bump, commit, tag, push, gh release, install,
#           verify installed)
# --------------------------------------------------------------------------- #

# Substrings that MUST never appear in a commit/tag/release message this tool
# generates. The repos flightdeck operates on forbid AI attribution.
_ATTRIBUTION_MARKERS = ("claude", "anthropic", "openai", "gpt", "copilot")


def _has_attribution(text: str) -> bool:
    """True if ``text`` carries any AI-attribution marker (case-insensitive)."""
    low = text.lower()
    return any(marker in low for marker in _ATTRIBUTION_MARKERS)


def _shell_quote_single(text: str) -> str:
    """Escape ``text`` for embedding inside a single-quoted shell string.

    POSIX rule: every ``'`` becomes ``'\\''`` (close-quote, escaped quote,
    reopen-quote). Nested content — including real newlines — is otherwise
    passed through untouched, so a multi-line release-note body stays readable
    as literal text inside the single-quoted argument.
    """
    return text.replace("'", "'\\''")


class ReleaseStepError(RuntimeError):
    """Raised when a release step's command exits non-zero.

    Carries the failing step's name (one of ``bump``, ``commit``, ``tag``,
    ``push``, ``gh-release``, ``install``) so the command layer can report
    exactly which step failed, plus the command that failed. Only raised in
    ``--apply`` mode, and only after the release actually stops — later steps
    are never attempted after a failure, so nothing is silently rolled back or
    skipped.

    Note the post-install verification phase is NOT raised from here: a version
    MISMATCH or an inability to re-read the installed version is a distinct
    FAILED/UNVERIFIED state returned via :class:`InstallOutcome`, not a failed
    shell command. Only the ``install`` command itself exiting non-zero raises.
    """

    def __init__(self, step: str, message: str, command: str = ""):
        super().__init__(message)
        self.step = step
        self.command = command


def execute(
    project,
    version: str,
    _run: Optional[Callable] = None,
    main_branch: str = DEFAULT_MAIN_BRANCH,
) -> ReleaseResult:
    """Execute the release steps in order; return a :class:`ReleaseResult`.

    ``version`` is already validated (greater than current) by
    :func:`preconditions` — an ``--apply`` caller MUST run preconditions first
    and only call execute when it returned no problems. We do not re-validate
    the semver shape; we only strip a leading ``v`` where convention requires.

    The steps, each emitted through the injectable ``_run`` in the project
    repo, are:

    1. **bump** — write the target version into ``<version_file>``.
    2. **commit** — ``git add <version_file> && git commit -m "release: v<version>"``.
    3. **tag** — annotated tag ``v<version>`` whose message is the CHANGELOG
       section for that version (``git tag -a vX -m '<section>'``).
    4. **push** — ``git push origin <branch> --tags``.
    5. **gh-release** — ``gh release create v<version>`` using the same
       CHANGELOG section as its notes.
    6. **install** — run the registry-declared ``install_cmd`` (an opaque
       command; flightdeck never assumes pip/npm/anything). Skipped with a
       plain message when no ``install_cmd`` is declared.
    7. **verify** — re-read the INSTALLED version via ``installed_version_cmd``
       and compare it to the released version (the "merged is not live" check).

    Steps 1-5 stop at the FIRST failure: the failing step raises
    :class:`ReleaseStepError` and no later command is issued. There is never a
    silent rollback — the caller sees exactly which step failed and the state
    reflects everything that ran up to it.

    Steps 6-7 are the post-install verification phase and return their result
    via :class:`InstallOutcome` (VERIFIED / UNVERIFIED / FAILED) rather than
    raising, because those are RELEASE outcomes to be reported loudly, not
    shell failures. The only exception: the ``install`` command itself exiting
    non-zero raises :class:`ReleaseStepError` (step ``install``), halting the
    release before any verification is attempted.

    The returned :class:`ReleaseResult` carries the completed step names in
    order and the install/verify outcome. A release is a clean success ONLY
    when ``result.verified`` is True — an UNVERIFIED or FAILED outcome must not
    read as a clean release, and the caller must surface it as such.
    """
    target = version.lstrip("v")
    repo = project.repo
    version_file = project.version_file or DEFAULT_VERSION_FILE
    completed: list[str] = []
    files_written: list[str] = []

    def _issue(step: str, cmd: str) -> None:
        """Dispatch ``cmd`` and raise ReleaseStepError if it fails."""
        cp = _dispatch(cmd, repo, _run)
        if cp.returncode != 0:
            detail = (cp.stderr or "").strip() or cp.returncode
            raise ReleaseStepError(
                step,
                f"release step {step!r} failed: {detail}",
                command=cmd,
            )

    # 1. bump the version sources. The VERSION file write is the existing step,
    #    kept exactly as-is (through _run). When the project ALSO declares a
    #    literal `[project] version` in pyproject.toml, that field is updated
    #    too — only that one line, formatting/comments byte-identical — so the
    #    tag and GitHub release never go out against a package that still
    #    identifies itself as the previous version. `files_written` records
    #    every repo-root file this step wrote, for the command layer to report.
    bump_cmd = f"sh -c 'printf \"%s\\n\" {target} > {version_file}'"
    _issue("bump", bump_cmd)
    files_written.append(version_file)
    completed.append("bump")

    if _write_pyproject_version(project, target):
        files_written.append(DEFAULT_PYPROJECT)

    # 2. commit the bump — every file the bump step actually wrote, not just
    #    the VERSION file. This was a real bug: v0.2.2 shipped with VERSION
    #    bumped but pyproject.toml's committed version left at the prior
    #    release, because this line only ever staged `version_file`.
    add_targets = " ".join(files_written)
    commit_cmd = f"git add {add_targets} && git commit -m \"release: v{target}\""
    _issue("commit", commit_cmd)
    completed.append("commit")

    # 3. annotated tag whose message is the CHANGELOG section for this version
    changelog_text = _read_repo_file(project, DEFAULT_CHANGELOG) or ""
    notes = _changelog_section(changelog_text, target) or ""
    tag_cmd = f"git tag -a v{target} -m '{_shell_quote_single(notes)}'"
    _issue("tag", tag_cmd)
    completed.append("tag")

    # 4. push the branch and the tag
    push_cmd = f"git push origin {main_branch} --tags"
    _issue("push", push_cmd)
    completed.append("push")

    # 5. create the GitHub release (CHANGELOG section as notes)
    gh_cmd = f"gh release create v{target} --notes '{_shell_quote_single(notes)}'"
    _issue("gh-release", gh_cmd)
    completed.append("gh-release")

    # 6. install the release (registry-declared opaque command — stops at first
    #    failure; a non-zero install raises and no verification is attempted)
    install_cmd = project.install_cmd
    if install_cmd:
        _issue("install", install_cmd)
        completed.append("install")

    # 7. verify the installed version is the just-released version
    outcome = verify_after_install(
        project, target, install_ran=bool(install_cmd), _run=_run
    )

    if outcome.install_ran and outcome.status == VERIFIED:
        completed.append("verify")

    return ReleaseResult(completed, outcome, tuple(files_written))

