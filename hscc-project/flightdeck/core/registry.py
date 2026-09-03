"""Project registry: repo <-> board <-> topic.

One entry per project, stored in ``~/.flightdeck/registry.yaml`` by default
(overridable with ``--registry PATH`` or ``save/load path=``).

Each project maps to the surfaces that report on it::

    - name:    project key (defaults to the repo basename when absent)
    - repo:    local git checkout path             (REQUIRED)
    - board:   Hermes kanban board slug            (optional -> "unknown")
    - session: the project's permanent Hermes session id. Set once and
               persisted server-side (via ensure_session) so a reinstall /
               new phone resolves the SAME ongoing session; optional ->
               deterministic default = project name
    - topic:   Telegram topic id                    (optional -> "unknown")
    - topic_name: expected Telegram topic NAME      (optional -> falls back to name)
    - verify:  shell command that proves it works   (optional -> "unknown")
    - roadmap: ROADMAP.md path within the repo      (optional -> "unknown")
    - install_cmd: opaque install command run by `release --apply` AFTER the
                   release is cut, so the installed artifact actually carries
                   the released version (optional -> "unknown"; absent means
                   the release can never be verified and is UNVERIFIED)
    - installed_version_cmd: shell command printing the deployed version
                            (optional -> "unknown")
    - version_file: file within the repo holding the source version
                    (optional -> default "VERSION")
    - deployed_at_cmd: shell command printing the unix timestamp of the last
                       deploy (optional -> "unknown")
    - depends_on: list of project names this project DEPENDS on (optional -> [])
                  e.g. a client app that consumes another project's API declares
                  that project here. Empty (the default) means self-contained.
                  A name that is not a registered project is a validation ERROR
                  on load (never silently tolerated), because an unresolvable
                  dependency would let cross-repo risk hide. A project with
                  dependents surfaces them in standup / review / dispatch as an
                  advisory (never blocking) note.


Only ``repo`` is required. Every other field degrades gracefully: an absent
field means "unknown", and a project is NEVER dropped from output because a
field is missing -- the silently omitted project is the exact failure this
tool exists to prevent.

``topic_name`` deserves a note: the audit (``flightdeck topics audit``)
detects when a topic's LIVE name was overwritten by comparing it against this
expected name. A project's key (``hscc``) and its human topic title (``HSCC
cluster``) are often different, so the registry records what the topic SHOULD
be called; when ``topic_name`` is absent it falls back to ``name``.
"""

from __future__ import annotations

import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

import yaml


class RegistryError(Exception):
    """Base class for registry errors."""


class ProjectNotFoundError(RegistryError):
    """A named project is not present in the registry."""


class MissingRepoError(RegistryError):
    """A project row has no ``repo`` field."""


class DuplicateProjectError(RegistryError):
    """A project with that name already exists."""


DEFAULT_REGISTRY = "~/.flightdeck/registry.yaml"

# Serialises every read-modify-write of the registry file. ensure_session uses
# it so two concurrent first-messages for the same project can never race: the
# first persists the session, the second sees it already exists and returns it.
# The relay and history path both funnel through this lock, so "two concurrent
# first-sends produce exactly one session" holds within this process.
_registry_lock = threading.Lock()

# Optional data fields -- absent means "unknown", never an error.
_OPTIONAL_FIELDS = (
    "board",
    "session",
    "topic",
    "topic_name",
    "verify",
    "roadmap",
    "install_cmd",
    "installed_version_cmd",
    "version_file",
    "deployed_at_cmd",
)


@dataclass
class Project:
    """A single registry entry."""

    name: str
    repo: str
    board: str | None = None
    session: str | None = None
    topic: int | None = None
    topic_name: str | None = None
    verify: str | None = None
    roadmap: str | None = None
    install_cmd: str | None = None
    installed_version_cmd: str | None = None
    version_file: str | None = None
    deployed_at_cmd: str | None = None
    depends_on: list[str] = field(default_factory=list)

    def missing_fields(self) -> list[str]:
        """Optional fields not set on this project (i.e. "unknown")."""
        return [f for f in _OPTIONAL_FIELDS if getattr(self, f) is None]


def _expand(path: str | None) -> str | None:
    """Expand a leading ``~`` to the user's home directory."""
    if path is None:
        return None
    return os.path.expanduser(path)


def _resolve_registry_path(path: str | None) -> Path:
    """Default the registry path to ~/.flightdeck/registry.yaml."""
    return Path(_expand(path if path is not None else DEFAULT_REGISTRY))


def _resolve_abs(path: str | None) -> str:
    """Normalise a path for comparison: expand ``~``, resolve symlinks, strip slashes.

    Mirrors core/kanban's ``_resolve_path`` so cwd detection and card
    attribution share the same path math (a symlinked checkout and its real
    path both reduce to the same string). Returns ``""`` for empty/None.
    ``realpath`` also collapses ``.``/``..`` and ``//`` for paths that do not
    exist, keeping synthetic test paths deterministic.
    """
    if not path:
        return ""
    expanded = _expand(str(path)) or ""
    try:
        resolved = os.path.realpath(expanded)
    except OSError:  # pragma: no cover - defensive
        resolved = expanded
    normalized = os.path.normpath(resolved)
    return normalized.rstrip("/") or normalized


def _name_for(row: dict, repo: str) -> str:
    """Derive a project name, falling back to the repo basename."""
    name = row.get("name")
    if name:
        return str(name).strip()
    return Path(_expand(repo)).name


def _parse_depends_on(raw) -> list[str]:
    """Normalise a project's ``depends_on`` value to a list[str].

    Absent/None -> []. A list of strings is accepted as-is (whitespace-trimmed).
    Anything else — a single string, a non-list element, a non-string element
    — raises a clear :class:`RegistryError`, because a malformed dependency is
    exactly what would silently let cross-repo risk hide (the failure this
    field exists to surface).
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise RegistryError(
            "project 'depends_on' must be a list of project names, "
            f"got {type(raw).__name__}: {raw!r}"
        )
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise RegistryError(
                "project 'depends_on' entries must be non-empty project-name "
                f"strings, got: {item!r}"
            )
        out.append(item.strip())
    return out


def _row_to_project(row: dict) -> Project:
    """Build a Project from a parsed yaml row.

    ``repo`` is required. Every other field is optional; a row with only
    ``repo`` set must load fine. A row with no repo is malformed and raises.
    """
    if not isinstance(row, dict):
        raise RegistryError(
            f"registry entries must be mappings, got {type(row).__name__}: {row!r}"
        )
    repo: str | None = row.get("repo")
    if not repo or not repo.strip():
        raise MissingRepoError(
            "every registry project needs a 'repo' field; found a row without one"
        )

    repo_str: str = repo.strip()
    expanded_repo = _expand(repo_str)
    assert expanded_repo is not None  # repo_str is non-None, so expansion is a str
    return Project(
        name=_name_for(row, repo_str),
        repo=expanded_repo,
        board=row.get("board"),
        session=row.get("session"),
        topic=row.get("topic"),
        topic_name=row.get("topic_name"),
        verify=row.get("verify"),
        roadmap=row.get("roadmap"),
        install_cmd=row.get("install_cmd"),
        installed_version_cmd=row.get("installed_version_cmd"),
        version_file=row.get("version_file"),
        deployed_at_cmd=row.get("deployed_at_cmd"),
        depends_on=_parse_depends_on(row.get("depends_on")),
    )


def load_registry(path: str | None = None) -> list[Project]:
    """Load the registry file and return its projects.

    A missing file is an empty registry (no error) -- the first ``add_project``
    creates it. Any malformed entry (missing ``repo``) raises rather than
    silently dropping the row, because dropping is the failure we guard against.
    """
    p = _resolve_registry_path(path)
    if not p.exists():
        return []

    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RegistryError(f"could not parse {p}: {exc}") from exc

    if raw is None:
        return []
    if not isinstance(raw, dict):
        raise RegistryError(f"registry root must be a mapping, got {type(raw).__name__}")
    if "projects" not in raw:
        return []

    projects = raw["projects"]
    if not isinstance(projects, list):
        raise RegistryError("registry 'projects' must be a list")

    loaded = [_row_to_project(row) for row in projects]
    _validate_dependencies(loaded)
    return loaded


def _validate_dependencies(projects: list[Project]) -> None:
    """Reject any ``depends_on`` reference to a project that is not registered.

    A dependency names another project in the registry. If that name does not
    resolve to a registered project, cross-repo risk would hide behind an
    unresolvable pointer — reject the registry load with a clear error naming
    every offending reference, rather than silently tolerating it. The default
    empty list needs no special case: a self-contained project has nothing to
    validate.
    """
    registered = {p.name for p in projects}
    problems: list[str] = []
    for p in projects:
        for dep in p.depends_on:
            if dep not in registered:
                problems.append(f"{p.name} depends_on {dep!r} (not a registered project)")
    if problems:
        joined = "; ".join(problems)
        raise RegistryError(
            "registry has unresolvable dependency reference(s): " + joined
        )


def dependents_for(project_name: str, projects: list[Project]) -> list[str]:
    """The project names that declare ``depends_on`` containing ``project_name``.

    Sorted, deduplicated. Empty when no project depends on it — the caller can
    then render nothing (no noise for the common self-contained case).
    """
    return sorted(
        {p.name for p in projects if project_name in (p.depends_on or [])}
    )


def dependent_notice(project_name: str, projects: list[Project]) -> str | None:
    """Advisory line naming a project's dependents, or None when none.

    ``2 dependent project(s): ecofire-app, efsdriver — consider verifying they``
    ``still work``. Returns None when no project depends on ``project_name``,
    so the common self-contained case prints nothing. The caller decides
    whether to use the note; with dependents present it is always advisory,
    never blocking.
    """
    deps = dependents_for(project_name, projects)
    if not deps:
        return None
    return (
        f"{len(deps)} dependent project(s): {', '.join(deps)} — "
        "consider verifying they still work"
    )


def load_ignored_topics(path: str | None = None) -> list[int]:
    """The registry's top-level ``ignored_topics:`` list (may be absent).

    Topic ids in this list are known-permanent (e.g. Telegram's built-in
    ``General`` topic) and are suppressed from commands that would otherwise
    report them forever. A missing file or missing key is an empty list — never
    an error. A malformed value raises rather than silently dropping the row.
    """
    p = _resolve_registry_path(path)
    if not p.exists():
        return []
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RegistryError(f"could not parse {p}: {exc}") from exc
    if not isinstance(raw, dict):
        return []
    value = raw.get("ignored_topics")
    if value is None:
        return []
    if not isinstance(value, list):
        raise RegistryError("registry 'ignored_topics' must be a list")
    return [int(v) for v in value]


def save_registry(
    projects: list[Project],
    path: str | None = None,
    ignored_topics: list[int] | None = None,
) -> None:
    """Persist projects to the registry file as yaml.

    Optional fields that are None are omitted from the output so the file
    round-trips cleanly and stays minimal. The top-level ``ignored_topics``
    list is carried through on every save so a project write never silently
    drops the ignore set; pass ``ignored_topics`` explicitly to set it.
    """
    p = _resolve_registry_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    rows = []
    for proj in projects:
        row: dict[str, object] = {"name": proj.name, "repo": proj.repo}
        for f in _OPTIONAL_FIELDS:
            value = getattr(proj, f)
            if value is not None:
                row[f] = value
        # depends_on is a list, not a single optional value: omit it when empty
        # so a self-contained project round-trips minimally (no `depends_on: []`
        # noise); write it only when the project actually declares a dependency.
        if proj.depends_on:
            row["depends_on"] = list(proj.depends_on)
        rows.append(row)

    doc = {"projects": rows}
    if ignored_topics is None:
        ignored_topics = load_ignored_topics(path)
    if ignored_topics:
        doc["ignored_topics"] = sorted(set(int(v) for v in ignored_topics))
    p.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def save_ignored_topics(ids: Iterable[int], path: str | None = None) -> None:
    """Set the registry's ``ignored_topics`` list (replacing prior contents).

    Rewrites the file through :func:`save_registry` so the existing projects
    are preserved untouched; only the ignore set changes.
    """
    projects = load_registry(path)
    save_registry(projects, path, ignored_topics=list(ids))


def add_project(
    name: str,
    repo: str,
    board: str | None = None,
    session: str | None = None,
    topic: int | None = None,
    verify: str | None = None,
    roadmap: str | None = None,
    install_cmd: str | None = None,
    installed_version_cmd: str | None = None,
    version_file: str | None = None,
    deployed_at_cmd: str | None = None,
    depends_on: list[str] | None = None,
    path: str | None = None,
) -> Project:
    """Add (or overwrite) a project. ``repo`` is required."""
    if not name or not str(name).strip():
        raise RegistryError("project 'name' is required")
    if not repo or not str(repo).strip():
        raise MissingRepoError("project 'repo' is required")

    projects = load_registry(path)
    existing = {p.name for p in projects}
    if name in existing:
        raise DuplicateProjectError(
            f"project {name!r} already exists; remove it first to replace it"
        )

    depends = _parse_depends_on(depends_on)
    # Validate the new project's dependencies against the registry it joins
    # (plus itself), so an invalid reference fails HERE with a clear error
    # rather than surfacing only on a later load.
    _validate_dependencies(projects + [Project(
        name=str(name).strip(), repo=str(repo).strip(), depends_on=depends,
    )])

    project = Project(
        name=str(name).strip(),
        repo=_expand(str(repo).strip()) or str(repo).strip(),
        board=board,
        session=session,
        topic=topic,
        verify=verify,
        roadmap=roadmap,
        install_cmd=install_cmd,
        installed_version_cmd=installed_version_cmd,
        version_file=version_file,
        deployed_at_cmd=deployed_at_cmd,
        depends_on=depends,
    )
    projects.append(project)
    save_registry(projects, path)
    return project


def remove_project(name: str, path: str | None = None) -> Project:
    """Remove a project by name. Raises if it is not present."""
    projects = load_registry(path)
    for i, proj in enumerate(projects):
        if proj.name == name:
            removed = projects.pop(i)
            save_registry(projects, path)
            return removed
    raise ProjectNotFoundError(f"no project named {name!r} in the registry")


def get_project(name: str, path: str | None = None) -> Project:
    """Return the project with the given name, or raise ProjectNotFoundError."""
    for proj in load_registry(path):
        if proj.name == name:
            return proj
    raise ProjectNotFoundError(f"no project named {name!r} in the registry")


def bind_topic(
    name: str,
    topic_id: int,
    path: str | None = None,
) -> Project:
    """Set the Telegram topic id for a project. Raises if the project is absent.

    Returns the updated Project. ``topic_id`` overrides any prior binding — the
    caller owns the intent, so this is a replace, not a no-op when already set.
    """
    projects = load_registry(path)
    for proj in projects:
        if proj.name == name:
            proj.topic = topic_id
            save_registry(projects, path)
            return proj
    raise ProjectNotFoundError(f"no project named {name!r} in the registry")


def unbind_topic(name: str, path: str | None = None) -> Project:
    """Clear the Telegram topic id for a project. Raises if the project is absent.

    Returns the updated Project with ``topic`` set to None (shown as "unknown").
    """
    projects = load_registry(path)
    for proj in projects:
        if proj.name == name:
            proj.topic = None
            save_registry(projects, path)
            return proj
    raise ProjectNotFoundError(f"no project named {name!r} in the registry")


def set_board(
    name: str,
    board: str | None,
    path: str | None = None,
) -> Project:
    """Set (or clear) the Hermes board slug for a project.

    Returns the updated Project. ``board`` overrides any prior binding and may
    be ``None`` to clear it. Raises :class:`ProjectNotFoundError` if the
    project is absent. This is a registry-only write — it never touches the
    board itself, so no card is ever moved or modified by it.
    """
    projects = load_registry(path)
    for proj in projects:
        if proj.name == name:
            proj.board = board
            save_registry(projects, path)
            return proj
    raise ProjectNotFoundError(f"no project named {name!r} in the registry")


def set_session(
    name: str,
    session: str | None,
    path: str | None = None,
) -> Project:
    """Set (or clear) the permanent Hermes session id for a project.

    Returns the updated Project. ``session`` overrides any prior id and may be
    ``None`` to clear it. Raises :class:`ProjectNotFoundError` if the project is
    absent. This is a registry-only write — it never touches the session itself,
    so no thread is lost or recreated by it.
    """
    with _registry_lock:
        projects = load_registry(path)
        for proj in projects:
            if proj.name == name:
                proj.session = session
                save_registry(projects, path)
                return proj
    raise ProjectNotFoundError(f"no project named {name!r} in the registry")


def ensure_session(name: str, path: str | None = None) -> str:
    """Return the project's permanent session id, persisting one if absent.

    The conversation thread for a project must survive a phone reinstall, so
    the session id is stored server-side in the registry, not handed to the
    client. If the project already carries a ``session`` (from a prior
    ``ensure_session`` or ``set_session``), it is returned unchanged — the id
    is immutable once the thread has started, so two devices always rejoin the
    SAME ongoing session.

    When no session is set yet (a project's very first message), the id is
    chosen deterministically as the project name and persisted: re-running is a
    stable no-op, and the same project always resolves to the same id.

    Idempotent + concurrency-safe: the read-modify-write runs under
    ``_registry_lock``, so two concurrent first-messages for the same project
    cannot create two sessions — the first persists, the second returns it.

    Raises :class:`ProjectNotFoundError` if the project is not in the registry.

    >>> ensure_session("hscc")  # first call persists "hscc"
    'hscc'
    >>> ensure_session("hscc")  # second call: unchanged, same id
    'hscc'
    """
    with _registry_lock:
        projects = load_registry(path)
        session_id: str | None = None
        for proj in projects:
            if proj.name == name:
                # Default deterministically to the project name (this is the
                # id the orchestrator already resolves to), so the first call
                # and every later call agree on the same durable id.
                session_id = proj.session or name
                if proj.session is None:
                    proj.session = session_id
                    save_registry(projects, path)
                return session_id
    raise ProjectNotFoundError(f"no project named {name!r} in the registry")


def detect_project_from_cwd(
    projects: list["Project"] | None,
    cwd: str | None = None,
) -> Project | None:
    """The registry project whose repo contains ``cwd`` (deepest match wins).

    Walks up from ``cwd`` (default ``os.getcwd()``), checking at each ancestor
    whether it exactly equals a registered project's expanded, normalised
    ``repo`` path. The first (deepest) ancestor that equals a repo wins — the
    closest, most specific match when ``cwd`` is nested inside two registered
    repos (e.g. a worktree ``<flightdeck>/.worktrees/<id>`` is under
    ``flightdeck``'s repo, so it detects ``flightdeck``, not whatever surrounds
    it).

    Returns ``None`` when no registered repo contains ``cwd`` — the caller
    then falls through to its existing no-project default unchanged. This is
    pure path math against the registry: no board, git, or network reads, so it
    is safe to call from any command and trivially testable.

    ``projects`` may be any iterable of objects exposing a ``repo`` attribute
    (typically ``registry.Project``).
    """
    start = _resolve_abs(os.getcwd() if cwd is None else str(cwd))
    if not start:
        return None

    # Normalise each repo once up front.
    repos: list[tuple[Project, str]] = []
    for proj in projects or []:
        repo = _resolve_abs(getattr(proj, "repo", None))
        if repo:
            repos.append((proj, repo))
    if not repos:
        return None

    current = start
    while True:
        for proj, repo in repos:
            if current == repo:
                return proj
        parent = os.path.dirname(current)
        if parent == current:  # reached the filesystem root
            return None
        current = parent


def project_for_cwd(
    cwd: str | None = None,
    *,
    projects: list["Project"] | None = None,
) -> Project | None:
    """The registered project whose ``repo`` is ``cwd`` or a parent of it.

    Given a cwd (default ``os.getcwd()``), find the registered project whose
    ``repo`` path is the cwd or one of its ancestors; the LONGEST-prefix
    (deepest / most specific) match wins when cwd is nested inside two
    registered repos. Returns ``None`` when cwd matches no project repo.

    ``projects`` may be passed explicitly for tests (defaults to loading the
    registry via :func:`load_registry`); ``cwd`` may be passed explicitly for
    tests (default ``os.getcwd()``). Both are injectable so the resolver is
    pure path math with no hidden ambient state.

    This is the canonical single resolver for the \"current project from cwd\"
    concern; it is a thin, named wrapper over :func:`detect_project_from_cwd`.
    """
    if projects is None:
        projects = load_registry()
    return detect_project_from_cwd(projects, cwd=cwd)


def resolve_project_arg(
    projects: list["Project"],
    explicit: str | None,
    *,
    cwd: str | None = None,
    _print=None,
) -> tuple[str | None, bool]:
    """Resolve a command's project selection: explicit arg, else detected.

    ``explicit`` is the project name the user actually typed (``None`` when
    omitted). When given it ALWAYS wins and is returned as-is (``detected``
    False) — an explicit choice is never silently overridden. When omitted,
    the project is auto-detected from ``cwd`` (deepest registered repo match);
    ``detected`` is True only when auto-detection produced a name.

    Returns ``(project_name_or_None, detected)``. ``project_name_or_None`` is
    ``None`` only when neither an explicit arg nor a cwd match was available —
    the caller then falls through to its existing no-project default.

    ``_print`` (default ``print``) is the sink for the one-line \"using project
    '<x>' (detected from cwd)\" note, so detection is never a silent, confusing
    default. Injectable so tests capture the note without touching stdout.
    """
    if explicit is not None and str(explicit):
        return str(explicit), False

    detected = detect_project_from_cwd(projects, cwd=cwd)
    if detected is None:
        return None, False

    sink = _print if _print is not None else print
    sink(f"using project '{detected.name}' (detected from cwd)")
    return detected.name, True
