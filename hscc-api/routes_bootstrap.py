"""HSCC HTTP API — new-project bootstrap wizard endpoints.

The phone-driven "new project" wizard (the ``t_7f90dbb7`` card) collapses the
operation's old multi-step CLI dance into ONE confirm-gated API call:

    flightdeck project new <name> --repo <path> --apply   (repo+board+registry)
    hscc.py orch <project>                                 (<project>-orch profile)
    (first chat --continue <session>)                      (<project> session)

``routes_orchestrator._ensure_session_exists`` already CREATEs a project's chat
session on first use; this module also CREATES it eagerly at bootstrap time so
the brand-new project is fully wired (profile + session + board + repo) before
the operator's first message.

Contract (mirrors routes_ops.py — see its module docstring for the house rules):

  * handlers are ``(server, ctx, query, body) -> (status, dict)``;
  * ``GET  /v1/projects/new/plan``  — READ-ONLY plan: validate the name/repo,
    pre-check every collision (registry duplicate, repo already a git repo,
    board exists, profile exists) and list the steps that WILL run, so the
    wizard's review screen shows exactly what the confirm will do and catches
    a bad name before the operator commits. Never mutates anything.
  * ``POST /v1/projects/new`` — MUTATING, confirm-gated (409 ``confirm_required``
    without ``confirm: true``). Runs the whole dance and returns a per-step
    report. An already-registered project NAME is a 409 ``project_exists`` (a
    creation wizard must not silently overwrite an existing project's registry
    entry; the operator picks a new name instead).

Backing (libraries, never CLI text-parsing):
  * plan/validation   -> ``flightdeck.core.project_lifecycle`` + the registry
  * repo+board+registry -> ``project_lifecycle.create_project``
  * orchestrator profile -> ``hscc_roles.orchestrators.ensure_orchestrator``
  * seeded session       -> ``routes_orchestrator._ensure_session_exists``

Test seam: every backing call goes through a ``_backing_*`` module function so
tests can monkeypatch them without touching a real registry, invoking a real
profile generator, or opening a live profile session DB.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from api_server import ApiError, ROUTES
from routes_ops import _parse_body, _require_confirm

# --------------------------------------------------------------------------- #
# Sibling-package loading (same sys.path pattern as routes_project/routes_orch)
# --------------------------------------------------------------------------- #
# Make the relocated flightdeck and the hscc-roles package importable exactly
# like the CLI and the orchestrator routes do: insert ``hscc-project/`` and
# ``hscc-roles/`` on sys.path once, then import the modules directly. The plugin
# dir itself is put on sys.path by the plugin's conftest when tests run alone.
for _subdir in ("hscc-project", "hscc-roles"):
    _dir = Path(__file__).resolve().parent.parent / _subdir
    if _dir.is_dir() and str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

# --------------------------------------------------------------------------- #
# Backing-call seam (monkeypatch these in tests)
# --------------------------------------------------------------------------- #

# A project name becomes the ``<project>-orch`` profile / ``<project>`` session
# / ``<project>`` board, all of which must be Hermes-safe identifiers: lowercase,
# alphanumeric plus ``-``/``_``, no spaces or characters that break a profile or
# a git/branch name.
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


def _validate_name(name: str) -> str | None:
    """Return ``None`` if ``name`` is a valid project name, else a reason."""
    if not name or not str(name).strip():
        return "name is required"
    n = str(name).strip()
    if len(n) > 40:
        return "name is too long (max 40 chars)"
    if not _NAME_RE.match(n):
        return ("name must start with a lowercase letter and contain only "
                "lowercase letters, digits, '-' or '_'")
    return None


def _backing_plan(name: str, repo: str, registry_path: str | None = None) -> dict:
    """Build the READ-ONLY plan dict for a name/repo.

    Pure validation + collision pre-check. Never mutates anything. The caller
    (the plan handler) resolves ``registry_path`` from ctx; ``None`` means
    "use the default registry" so this fn is pure enough for direct tests and
    is also monkeypatchable.
    """
    name = (name or "").strip()
    repo = (repo or "").strip()
    name_ok = _validate_name(name)
    repo_expanded = os.path.expanduser(repo) if repo else repo

    # --- validations ------------------------------------------------------ #
    name_valid = name_ok is None
    repo_given = bool(repo_expanded)
    repo_path_valid = repo_given  # a bare path is acceptable; -orch mints it

    validations = {
        "name_valid": name_valid,
        "name_error": name_ok,
        "repo_given": repo_given,
    }

    # --- collisions (only meaningful once name/repo are syntactically valid) #
    collisions = {
        "registry_has_project": False,
        "repo_is_git": False,
        "board_exists": False,
        "profile_exists": False,
    }
    if name_valid:
        collisions["registry_has_project"] = _registry_has_project(
            name, registry_path)
        collisions["board_exists"] = _board_exists(name)
        collisions["profile_exists"] = _profile_exists(f"{name}-orch")
    if repo_given:
        collisions["repo_is_git"] = _is_git_repo(repo_expanded)

    # --- blockers: reasons the wizard must NOT proceed -------------------- #
    blockers = []
    if not name_valid:
        blockers.append(name_ok or "invalid name")
    if collisions["registry_has_project"]:
        blockers.append(f"project {name!r} is already registered")
    if collisions["profile_exists"]:
        blockers.append(f"orchestrator profile {name}-orch already exists")

    ready = not blockers
    steps = ["repo", "board", "orchestrator", "session"] if not collisions["registry_has_project"] else []

    return {
        "name": name,
        "repo": repo_expanded,
        "ready": ready,
        "validations": validations,
        "collisions": collisions,
        "blockers": blockers,
        "steps": steps,
        "would_create": {
            "profile": f"{name}-orch" if name else None,
            "session": name or None,
            "board": name or None,
        } if name else {},
    }


def _registry_has_project(name: str, registry_path: str | None) -> bool:
    """True if ``name`` is already a project in the flightdeck registry."""
    try:
        from orchestrators import list_registry_projects
        return name in list_registry_projects(path=registry_path)
    except Exception:
        # Cannot read the registry — do not guess. Report no collision so the
        # POST's own create_project surfaces the truth if it matters.
        return False


def _board_exists(board: str) -> bool:
    """True if a kanban board named ``board`` already exists."""
    try:
        from flightdeck.core import project_lifecycle
        return project_lifecycle._resolve_kanban(None).board_exists(board)
    except Exception:
        return False


def _profile_exists(profile: str) -> bool:
    """True if the Hermes profile ``profile`` already exists on disk."""
    try:
        from hermes_cli import profiles
        canon = profiles.normalize_profile_name(profile)
        return profiles.profile_exists(canon)
    except Exception:
        return False


def _is_git_repo(repo: str) -> bool:
    """True if ``repo`` is already a git work tree (or can't be reached)."""
    try:
        from flightdeck.core import project_lifecycle
        return project_lifecycle.is_git_repo(repo)
    except Exception:
        return False


# --- POST execute seams (wrapped so tests can monkeypatch without RTE) ------- #

def _backing_create_project(name, repo, registry_path, github, private):
    from flightdeck.core import project_lifecycle
    return project_lifecycle.create_project(
        name,
        repo=repo,
        registry_path=registry_path,
        github=github,
        private=private,
        include_topic=False,   # a phone-driven bootstrap does not mint a
        # Telegram topic by default — the operator can
        # add chat wiring later via the board.
    )


def _backing_ensure_orchestrator(name, registry_path, base_identity=""):
    from orchestrators import ensure_orchestrator
    return ensure_orchestrator(
        name, base_identity=base_identity, path=registry_path)


def _backing_ensure_session(profile, session):
    from routes_orchestrator import _ensure_session_exists
    return _ensure_session_exists(profile, session)


# --------------------------------------------------------------------------- #
# speak helpers (design §B) — pure
# --------------------------------------------------------------------------- #

def _speak_plan(data: dict) -> str:
    """§B: ready ? \"Ready to create <name>.

(...)\" : \"<n> blocker(s).\" .
    """
    if not data.get("ready"):
        blockers = data.get("blockers") or []
        return (f"{len(blockers)} blocker{'s' if len(blockers) != 1 else ''}: "
                + "; ".join(blockers) + ".")
    name = data.get("name") or "project"
    return (f"Ready to create project {name!r}. "
            f"{len(data.get('steps') or [])} steps will run: "
            + ", ".join(data.get("steps") or []) + ".")


def _speak_create(data: dict) -> str:
    """§B: ok ? \"Created <name>.\" : \"Created <name> partially — see steps.\"."""
    name = data.get("name") or "project"
    if data.get("ok"):
        return f"Project {name!r} created with its orchestrator and session."
    return f"Project {name!r} was created only partially — review the steps."


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #

def handle_plan(server, ctx, query, body):
    """GET /v1/projects/new/plan — READ-ONLY plan for a prospective project."""
    name = query.get("name", [""])[0] if isinstance(query, dict) else ""
    repo = query.get("repo", [""])[0] if isinstance(query, dict) else ""
    try:
        from routes_project import _registry_path
        registry_path = _registry_path(ctx)
    except Exception:
        registry_path = None
    try:
        data = _backing_plan(name, repo, registry_path)
    except Exception:
        return 200, {"speak": "Project plan unavailable."}
    if not isinstance(data, dict):
        return 200, {"speak": "Project plan unavailable."}
    data["speak"] = _speak_plan(data)
    return 200, data


def handle_create(server, ctx, query, body):
    """POST /v1/projects/new — create a project end-to-end (confirm-gated)."""
    data = _parse_body(body)
    _require_confirm(data, "create a new project")
    name = str(data.get("name") or "").strip()
    repo = str(data.get("repo") or "").strip()
    github = bool(data.get("github"))
    private = bool(data.get("private"))

    if not name:
        raise ApiError(400, "bad_request", "project 'name' is required",
                       "A project name is required.")
    name_err = _validate_name(name)
    if name_err:
        raise ApiError(400, "bad_request", name_err, name_err.capitalize())
    if not repo:
        raise ApiError(400, "bad_request", "project 'repo' is required",
                       "A repo path is required.")

    try:
        from routes_project import _registry_path
        registry_path = _registry_path(ctx)
    except Exception:
        registry_path = None

    # Refuse to silently overwrite an existing project: a creation wizard must
    # surface "already registered" and make the operator pick a new name,
    # rather than upserting over an existing project's registry row.
    if _backing_plan(name, repo, registry_path).get("collisions", {}) \
            .get("registry_has_project"):
        raise ApiError(
            409, "project_exists",
            f"project {name!r} is already registered; choose a new name",
            f"Project {name!r} already exists.")

    # 1. repo + board + registry (via flightdeck lifecycle).
    steps = []
    try:
        created = _backing_create_project(
            name, repo, registry_path, github, private)
        steps.extend(created.get("steps") or [])
        repo_ok = created.get("ok", False)
    except Exception as exc:
        raise ApiError(502, "project_create_failed",
                       f"project creation failed: {exc}",
                       "Project creation failed.")

    # 2. orchestrator profile (<name>-orch).
    orch_step = {"id": "orchestrator", "status": "ok",
                 "detail": f"profile {name}-orch"}
    profile = f"{name}-orch"
    try:
        orch = _backing_ensure_orchestrator(name, registry_path)
        if orch.get("changed"):
            orch_step["detail"] += " (created)"
        else:
            orch_step["detail"] += " (already existed)"
    except Exception as exc:
        orch_step["status"] = "failed"
        orch_step["detail"] = f"{name}-orch profile not ensured: {exc}"
    steps.append(orch_step)

    # 3. seeded session (<name> on <name>-orch).
    sess_step = {"id": "session", "status": "ok",
                 "detail": f"session {name} on {profile}"}
    try:
        seeded = _backing_ensure_session(profile, name)
        if seeded:
            sess_step["detail"] = f"seeded session {seeded.get('created_session')}"
        else:
            sess_step["detail"] = f"session {name} (already existed or unresolvable)"
    except Exception as exc:
        sess_step["status"] = "failed"
        sess_step["detail"] = f"session {name} not seeded: {exc}"
    steps.append(sess_step)

    all_ok = repo_ok and all(
        s.get("status") != "failed" for s in steps)

    payload = {
        "name": name,
        "repo": repo,
        "board": name,
        "profile": profile,
        "session": name,
        "steps": steps,
        "ok": all_ok,
        "message": "project created" if all_ok else "project created partially",
        "speak": _speak_create({"name": name, "ok": all_ok}),
    }
    return (200 if all_ok else 202), payload


# --------------------------------------------------------------------------- #
# Route registration (import side-effect; loaded by api_server.py)
# --------------------------------------------------------------------------- #

ROUTES.append(("GET", re.compile(r"^/v1/projects/new/plan$"), handle_plan))
ROUTES.append(("POST", re.compile(r"^/v1/projects/new$"), handle_create))
