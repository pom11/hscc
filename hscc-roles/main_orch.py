"""The main orchestrator identity: one name, stored once in hscc state.

The 2026-11-19 design replaces the two old cluster-wide agents
(``general-orch`` and the ``orchestrator`` role profile) with ONE main
orchestrator named at bootstrap, default ``general``. This module is the
single resolver every component reads:

    main_name()     -> the stored name, or "general" when nothing is stored
    main_profile()  -> the profile name (the chosen name itself)
    main_board()    -> the board slug (the chosen name itself)

The name is persisted in ``~/.hscc/main-orchestrator.json`` — a small JSON
state document written atomically, matching the other ``~/.hscc`` state files
(``serving.json``, ``api.json``, ``cluster.json``). ``HSCC_DIR`` relocates the
root so tests and sandboxed bootstrap runs never touch the operator's state.

Re-running bootstrap must never change a stored name: :func:`store_name`
refuses to overwrite one unless ``overwrite=True`` is explicitly requested by a
future ``hscc main rename`` verb.

Validation (the interview contract): lowercase slug ``[a-z0-9-]`` (no leading
or trailing hyphen, no doubled hyphen), not a role-profile name from
``hscc-roles/roles/*.yaml``, and not itself an existing ``<project>-orch``
profile name (the ``-orch`` suffix is the per-project namespace). A custom
name must also not land on an existing project's ``<name>-orch`` identity; the
default ``general`` is exempt because every pre-migration install still has
``general-orch`` on disk (retired, never deleted). The legacy ``orchestrator``
role profile is likewise kept on disk for existing installs; it is simply no
longer the source of truth.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import yaml

DEFAULT_NAME = "general"
STATE_FILENAME = "main-orchestrator.json"

#: The legacy cluster-authority role profile. Kept on disk for existing
#: installs (M2 retires it); never returned by the resolver for a new name.
LEGACY_ORCHESTRATOR_ROLE = "orchestrator"
#: The legacy catch-all identities, accepted by ``resolve_orchestrator`` so an
#: explicit old name still reaches the profile that exists on that install.
LEGACY_GENERAL_PROFILE = "general-orch"
LEGACY_GENERAL_SESSION = "general"
LEGACY_GENERAL_BOARD = "default"

#: Marker every retired profile's SOUL carries. The migration and the generator
#: both key idempotence on it (never the full heading, so a future rename cannot
#: duplicate the block).
RETIRED_MARKER = "retired — use"

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class MainOrchestratorError(Exception):
    """An invalid or conflicting main-orchestrator name."""


# --------------------------------------------------------------------------- #
# State location + read/write
# --------------------------------------------------------------------------- #

def hscc_root(hscc_dir=None) -> str:
    """The ``~/.hscc`` root, honouring an explicit arg then ``HSCC_DIR``."""
    root = hscc_dir or os.environ.get("HSCC_DIR") or "~/.hscc"
    return os.path.expanduser(root)


def state_path(hscc_dir=None) -> str:
    return os.path.join(hscc_root(hscc_dir), STATE_FILENAME)


def _read_doc(hscc_dir=None) -> dict:
    path = state_path(hscc_dir)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def stored_name(hscc_dir=None):
    """The persisted name, or ``None`` when bootstrap has not stored one."""
    name = _read_doc(hscc_dir).get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None


def main_name(hscc_dir=None) -> str:
    """The main orchestrator's name; ``general`` until bootstrap stores one."""
    return stored_name(hscc_dir) or DEFAULT_NAME


def main_profile(hscc_dir=None) -> str:
    """The main orchestrator's Hermes profile name (the chosen name itself)."""
    return stored_name(hscc_dir) or DEFAULT_NAME


def main_board(hscc_dir=None) -> str:
    """The main orchestrator's board slug (the chosen name itself)."""
    return stored_name(hscc_dir) or DEFAULT_NAME


def main_identity(hscc_dir=None) -> dict:
    """The resolver's identity dict for the main orchestrator."""
    name = main_name(hscc_dir)
    return {
        "project": name,
        "profile": main_profile(hscc_dir),
        "session": name,
        "board": main_board(hscc_dir),
        "repo": None,
    }


def legacy_general_profile() -> str:
    """The legacy catch-all's profile name (kept on disk, retired).

    A function, not a bare constant reference, so modules that migrate the
    legacy profile never spell the old name into new code: the resolver stays
    the one owner of it.
    """
    return LEGACY_GENERAL_PROFILE


def retirement_block(name=None) -> str:
    """The SOUL block that retires a legacy profile in favour of the main one.

    Kept here (the resolver owns the identity) so the generator and the
    migration writer emit byte-identical text. Contains
    :data:`RETIRED_MARKER`, which both writers use as their idempotence key.
    """
    main = name or main_name()
    block = (
        f"## Retired — use `{main}`\n\n"
        f"This profile is **{RETIRED_MARKER} {main}**. The ONE main "
        f"orchestrator `{main}` now holds the cluster-wide authority and its "
        "own board; it replaces both the old cluster-wide role and the general "
        "board duties. This profile is kept on disk only for history — do not "
        "create, dispatch or claim work here.\n"
    )
    return block


def store_name(name, *, hscc_dir=None, overwrite=False,
               roles_dir=None, profiles_dir=None, registry_path=None) -> str:
    """Validate + persist ``name``; return the stored name.

    Refuses to change an already-stored name unless ``overwrite=True`` — the
    bootstrap idempotence contract. An identical re-store is a harmless no-op.
    """
    name = validate_name(name, roles_dir=roles_dir, profiles_dir=profiles_dir,
                         registry_path=registry_path)
    existing = stored_name(hscc_dir)
    if existing and not overwrite:
        if existing == name:
            return existing
        raise MainOrchestratorError(
            f"main orchestrator is already stored as {existing!r}; "
            "refusing to change it")
    root = hscc_root(hscc_dir)
    os.makedirs(root, exist_ok=True)
    path = state_path(hscc_dir)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"name": name}, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return name


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

def _default_roles_dir(roles_dir=None) -> str:
    if roles_dir:
        return os.path.expanduser(roles_dir)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "roles")


def _role_names(roles_dir=None) -> set:
    """Names from ``hscc-roles/roles/*.yaml`` (filename + declared name)."""
    names = set()
    directory = _default_roles_dir(roles_dir)
    try:
        filenames = os.listdir(directory)
    except OSError:
        return names
    for fn in filenames:
        if not fn.endswith((".yaml", ".yml")):
            continue
        names.add(fn.rsplit(".", 1)[0])
        try:
            with open(os.path.join(directory, fn), encoding="utf-8") as f:
                doc = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(doc, dict) and isinstance(doc.get("name"), str):
            names.add(doc["name"])
    return names


def default_profiles_dir(profiles_dir=None) -> str:
    """The Hermes profiles root, accepting both root-scoped and profile-scoped
    ``HERMES_HOME`` values (Hermes exports the latter to every session)."""
    if profiles_dir:
        return os.path.expanduser(profiles_dir)
    home = os.environ.get("HERMES_HOME") or "~/.hermes"
    home = os.path.expanduser(home)
    if os.path.basename(os.path.dirname(home)) == "profiles":
        return os.path.dirname(home)
    if os.path.basename(home) == "profiles":
        return home
    return os.path.join(home, "profiles")


def _registry_projects(registry_path=None) -> list:
    path = os.path.expanduser(
        registry_path or os.environ.get("HSCC_REGISTRY")
        or "~/.flightdeck/registry.yaml")
    try:
        with open(path, encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}
    except (FileNotFoundError, OSError, yaml.YAMLError):
        return []
    rows = doc.get("projects") if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        return []
    return [str(r["name"]) for r in rows
            if isinstance(r, dict) and r.get("name")]


def validate_name(name, *, roles_dir=None, profiles_dir=None,
                  registry_path=None) -> str:
    """Return ``name`` when it is a legal main-orchestrator name.

    Raises :class:`MainOrchestratorError` with an operator-readable reason
    otherwise.
    """
    candidate = (name or "").strip() if isinstance(name, str) else ""
    if not candidate:
        raise MainOrchestratorError("name is empty")
    if not _SLUG_RE.match(candidate):
        raise MainOrchestratorError(
            f"{candidate!r} is not a lowercase slug ([a-z0-9-], no leading/"
            "trailing or doubled hyphen)")
    if candidate.endswith("-orch"):
        raise MainOrchestratorError(
            f"{candidate!r} ends with '-orch', which belongs to the per-project "
            "orchestrator profiles")
    roles = _role_names(roles_dir)
    if candidate in roles:
        raise MainOrchestratorError(
            f"{candidate!r} is a role profile name "
            f"({', '.join(sorted(roles)[:3])}, ...)")
    if (candidate != DEFAULT_NAME
            and candidate in _registry_projects(registry_path)):
        raise MainOrchestratorError(
            f"{candidate!r} is a project name; its <project>-orch profile "
            "already owns that identity")
    # A custom name must not land on an existing project orchestrator's
    # identity either. ``general`` is exempt BY DESIGN: every pre-migration
    # install still has ``general-orch`` on disk (kept, retired), and the
    # default name must remain storeable there.
    orch_profile = f"{candidate}-orch"
    if (candidate != DEFAULT_NAME
            and os.path.isdir(os.path.join(default_profiles_dir(profiles_dir),
                                           orch_profile))):
        raise MainOrchestratorError(
            f"{orch_profile!r} already exists as a per-project orchestrator")
    return candidate


# --------------------------------------------------------------------------- #
# CLI (bootstrap calls this; tests drive it directly)
# --------------------------------------------------------------------------- #

def _cli(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: main_orch.py {name|profile|board|stored|validate|store} "
              "[NAME]", file=sys.stderr)
        return 2
    cmd, rest = args[0], args[1:]
    try:
        if cmd == "name":
            print(main_name())
        elif cmd == "profile":
            print(main_profile())
        elif cmd == "board":
            print(main_board())
        elif cmd == "stored":
            name = stored_name()
            if not name:
                return 1
            print(name)
        elif cmd == "validate":
            print(validate_name(rest[0] if rest else ""))
        elif cmd == "store":
            print(store_name(rest[0] if rest else ""))
        else:
            print(f"unknown command: {cmd}", file=sys.stderr)
            return 2
    except (MainOrchestratorError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    raise SystemExit(_cli())
