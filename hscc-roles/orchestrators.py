"""Per-project orchestrator profiles: resolver + idempotent generator.

One project = one orchestrator profile + one long-lived session + one memory
+ one kanban board. The boards already exist (one per project in the flightdeck
registry, plus a catch-all ``default`` board); this module adds the
profile+session layer that was missing.

Conventions (operator-defined, DO NOT invent alternatives):
  * profile name: ``<project>-orch``            (e.g. ``ecofire-app-orch``)
  * session name: ``<project>``                  (``hermes -p <P>-orch chat --continue <P>``)
  * board:        the project's EXISTING board from the registry
  * catch-all:    ``general`` -> ``general-orch`` / ``general`` / ``default``
                  (repo=None, not repo-scoped) — the DEFAULT when no project
                  is specified.

The registry at ``~/.flightdeck/registry.yaml`` is the source of truth for
which projects exist — we read it, never hardcode a project list. The path is
overridable via ``path=`` / the ``HSCC_REGISTRY`` env var so tests can point
at a fixture without touching the real registry or profile tree.
"""

from __future__ import annotations

import os
from typing import Optional

import yaml

import generator

# Registry default matches flightdeck's DEFAULT_REGISTRY (~/.flightdeck/registry.yaml).
# Overridable with HSCC_REGISTRY so tests/provisioning can target a fixture.
REGISTRY_PATH = os.environ.get("HSCC_REGISTRY", "~/.flightdeck/registry.yaml")

# The catch-all orchestrator (not tied to any project).
GENERAL_PROFILE = "general-orch"
GENERAL_SESSION = "general"
GENERAL_BOARD = "default"
GENERAL_REPO = None


class OrchestratorError(Exception):
    """Base error for the orchestrator resolver/generator."""


class UnknownProjectError(OrchestratorError):
    """A project name is neither a registry project nor the ``general`` sentinel."""


def _registry_path(path: Optional[str]) -> str:
    """Resolve the registry path: explicit arg > HSCC_REGISTRY env > default."""
    if path:
        return os.path.expanduser(path)
    return os.path.expanduser(REGISTRY_PATH)


def _load_projects(path: Optional[str]) -> list[dict]:
    """Parse the registry's ``projects:`` list. Missing file -> empty list."""
    p = _registry_path(path)
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    projects = data.get("projects") if isinstance(data, dict) else None
    return projects if isinstance(projects, list) else []


def list_registry_projects(path: Optional[str] = None) -> list[str]:
    """Return the name of every project in the registry.

    Missing/unreadable registry -> empty list (never raises), so a caller that
    wants to provision "every project plus the general catch-all" degrades to
    just the catch-all on a bare machine instead of dying.
    """
    names = []
    for row in _load_projects(path):
        name = row.get("name")
        if name:
            names.append(str(name))
    return names


def resolve_orchestrator(project: Optional[str] = None, path: Optional[str] = None) -> dict:
    """Resolve a project to its orchestrator identity.

    Returns a dict ``{"profile", "session", "board", "repo"}`` where ``repo``
    is ``None`` for the project-less ``general`` orchestrator.

    ``project`` may be:
      * ``None`` or ``"general"`` -> the catch-all orchestrator
        (``general-orch`` / ``general`` / ``default`` / repo=None).
      * a real registry project name -> ``<project>-orch`` / ``<project>`` /
        the project's registry ``board`` / the project's repo path.

    Raises :class:`UnknownProjectError` for any other name.
    """
    if not project or project == GENERAL_SESSION:
        return {
            "project": GENERAL_SESSION,
            "profile": GENERAL_PROFILE,
            "session": GENERAL_SESSION,
            "board": GENERAL_BOARD,
            "repo": GENERAL_REPO,
        }

    name = str(project).strip()
    for row in _load_projects(path):
        if row.get("name") == name:
            board = row.get("board")
            # An orchestrator routes through a board; a real project without one
            # is a contract error, not something to silently guess at.
            if not board:
                raise OrchestratorError(
                    f"project {name!r} has no 'board' in the registry; "
                    "cannot resolve an orchestrator for it"
                )
            return {
                "project": name,
                "profile": f"{name}-orch",
                "session": name,
                "board": str(board),
                "repo": row.get("repo"),
            }

    raise UnknownProjectError(
        f"unknown project {name!r}: not in the registry and not the "
        f"'general' sentinel"
    )


def _project_orch_spec(project: str, resolved: dict) -> dict:
    """Build the role spec for a project's orchestrator from resolved facts.

    The spec's identity states which project it orchestrates, its repo path,
    and its board — all pulled from the registry, never hardcoded. ``general``
    carries no repo, so its SOUL omits the repo line.
    """
    profile = resolved["profile"]
    board = resolved["board"]
    repo = resolved["repo"]

    lines = [f"You are the orchestrator for the **{project}** project."]
    if repo:
        lines.append(f"Its repo is at `{repo}`.")
    lines.append(
        f"You route work through the `{board}` kanban board and hold sole "
        f"authority over that board — you decompose ideas into structured cards, "
        f"dispatch them to specialist workers, and do NOT do project work "
        f"yourself."
    )
    identity = " ".join(lines)

    routing = (
        f"Claim tasks involving decomposing and dispatching work for the "
        f"**{project}** project onto the `{board}` board, coordinating "
        f"specialist workers on the project, and any project-level orchestration. "
        f"Holds sole authority over the `{board}` board. Do NOT claim "
        f"implementation work — route it to the appropriate specialist profile."
    )
    return {
        "name": profile,
        "identity": identity + "\n",
        "routing_description": routing,
        # Match the existing `orchestrator` role's preload + tier.
        "preload_skills": ["brainstorming", "writing-plans"],
        "model_tier": "strong",
    }


def ensure_orchestrator(
    project: Optional[str] = None,
    base_identity: str = "",
    path: Optional[str] = None,
) -> dict:
    """Idempotently ensure a project's orchestrator profile exists.

    Re-running is a no-op: an existing profile is left untouched (its memory
    and sessions are never clobbered). Built on the existing ``hscc-roles``
    ``generator.generate_profile`` machinery — the same path that mints the
    shipped role profiles. Returns the resolved identity dict (see
    :func:`resolve_orchestrator`) with a ``changed`` flag.
    """
    resolved = resolve_orchestrator(project, path=path)
    spec = _project_orch_spec(resolved["project"], resolved)
    changed = generator.generate_profile(spec, base_identity=base_identity)
    return {**resolved, "changed": changed}
