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
            # The session id may be persisted in the registry (ensure_session /
            # set_session) so a reinstall resolves the SAME ongoing thread.
            # Falls back deterministically to the project name, which matches
            # ensure_session's default — relay and history always agree.
            return {
                "project": name,
                "profile": f"{name}-orch",
                "session": row.get("session") or name,
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


def seed_review_skills_for(profile_dir: str) -> dict:
    """Guarded install of the review-lane skills into ONE orchestrator profile
    home (B1, round-2 review — the `orch`/`orch-all` hole).

    A profile that `orch`/`orch-all` has just created has an EMPTY skills
    tree, and the dispatcher force-injects ``sdlc-review`` for review-lane
    spawns to ANY assignee (kanban_db_dispatch) — so a freshly provisioned
    orchestrator that nobody seeded dies on its very first review card with
    'Unknown skill(s): sdlc-review', exactly the card's bug class. The
    bootstrap stage covers the batch case; this covers profiles created LATER
    by `hscc.py orch <new-project>` (or the API's bootstrap route), where no
    later bootstrap run is guaranteed.

    Delegates every write decision to ``install_review_skill.plan_home`` — the
    same collision-guarded, idempotent, never-delete, always-back-up path the
    bootstrap stage uses (a name that resolves elsewhere is skipped, never
    duplicated). Best-effort by design: provisioning an orchestrator must
    never fail because skill seeding could not run (half-deployed tree), so a
    failure is REPORTED in the returned dict, not raised — and doctor's
    reviewer-readiness finding surfaces it on the next read.

    Returns a small report ``{"ok": bool, "skills": {name: action}, ...}``.
    """
    from pathlib import Path as _Path
    import sys as _sys
    profile_home = _Path(profile_dir)
    name = profile_home.name
    report = {"ok": True, "skills": {}}
    try:
        # Sibling plugin dir (deployed layout: ~/.hermes/plugins/hscc-bootstrap
        # next to hscc-roles; repo layout: <repo>/hscc-bootstrap). String
        # concat, NOT os.path.join(dir, os.sep, ...) — os.sep is absolute and
        # would restart the path at '/'.
        _sibling = (os.path.dirname(os.path.abspath(__file__))
                    + os.sep + ".." + os.sep + "hscc-bootstrap")
        if _sibling not in _sys.path:
            _sys.path.insert(0, _sibling)
        import install_review_skill as irs  # noqa: F401 (lazy sibling import)
    except ImportError as exc:  # pragma: no cover - half-deployed tree only
        return {"ok": False, "skills": {},
                "error": f"install_review_skill unavailable: {exc}"}
    if not skill_guard_is_reviewer(name):
        # Non-reviewer homes are never seeded (the card forbids installing
        # into profiles that cannot review); orchestrators are all reviewers.
        report["skills"]["_"] = "skipped-not-reviewer"
        return report
    # Same B3 policy as install_review_skill(): inside a REVIEWER-CAPABLE home
    # the .no-bundled-skills marker does NOT bind — create_profile
    # (no_skills=True) (generator.py:470) is what minted it, so it is hscc's own
    # footprint rather than an operator opt-out, and honouring it would leave a
    # freshly minted orchestrator unable to answer the dispatcher's
    # force-injected review spawn. The card's acceptance needs all THREE names
    # resolvable on the *-orch set, so all three are seeded here. (The marker
    # still binds for NON-reviewer homes — install_review_skill's caller decides
    # that, and every profile reaching this function is already gated as a
    # reviewer by skill_guard_is_reviewer above.)
    for skill in irs.INSTALLABLE:
        bundle = irs.bundle_source(skill)
        if bundle is None or not (bundle / "SKILL.md").is_file():
            report["ok"] = False
            report["skills"][skill] = "missing-bundle"
            continue
        try:
            plan = irs.plan_home(
                name, str(profile_home / "skills"), skill, bundle,
                dry_run=False, stamp="orch", profile_home=profile_home,
                reviewer_only=True, honor_opt_out=False)
            report["skills"][skill] = plan["action"]
        except OSError as exc:
            report["ok"] = False
            report["skills"][skill] = f"error: {exc}"
    return report


def skill_guard_is_reviewer(profile_name: str) -> bool:
    """Reviewer-capability via the shared rule (skill_guard), falling back to
    the local ``-orch`` convention if the bootstrap module is unreachable."""
    import sys as _sys
    try:
        _sibling = (os.path.dirname(os.path.abspath(__file__))
                    + os.sep + ".." + os.sep + "hscc-bootstrap")
        if _sibling not in _sys.path:
            _sys.path.insert(0, _sibling)
        import skill_guard  # noqa: F401 (lazy sibling import)
        return skill_guard.is_reviewer_capable(profile_name)
    except ImportError:  # pragma: no cover - half-deployed tree only
        return profile_name.endswith("-orch") or profile_name == "reviewer"


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
    :func:`resolve_orchestrator`) with a ``changed`` flag and a
    ``review_skill`` seeding report (B1: the dispatcher force-injects
    ``sdlc-review`` for review-lane spawns to ANY profile, so a freshly
    provisioned orchestrator must resolve it before it can hold a review
    card — guarded, idempotent, best-effort; see
    :func:`seed_review_skills_for`).
    """
    resolved = resolve_orchestrator(project, path=path)
    spec = _project_orch_spec(resolved["project"], resolved)
    changed = generator.generate_profile(spec, base_identity=base_identity)
    # Same rolelib object generate_profile uses (tests monkeypatch it).
    seed = seed_review_skills_for(
        os.path.join(generator.rolelib.PROFILES_DIR, resolved["profile"]))
    return {**resolved, "changed": changed, "review_skill": seed}
