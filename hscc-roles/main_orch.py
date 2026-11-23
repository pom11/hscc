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
# The main-only cluster surface (t_e98c6ad4)
# --------------------------------------------------------------------------- #
#
# Why the plugin-name policy lives here, next to the identity resolver: the
# operator's ruling (docs/audits/2026-11-23-main-only-surface.md §6 D-A/D-B/D-C,
# verdict ~/fleet-check/verdict-main-only.txt) makes the cluster surface a
# function of ONE fact — "is this Hermes home the main orchestrator's home?" —
# and this module is the single owner of that fact. A second copy of the tier
# rule (in bootstrap, in a plugin, in a test) is a second resolver, which is
# exactly what D-A forbids: the name must never be hard-coded anywhere except
# the ``DEFAULT_NAME`` above.
#
# The tier is deliberately a *config* answer (what ``plugins.enabled`` may
# carry) and a *registration* answer (whether tools register) in one place, so
# the bootstrap writer and the plugins' ``register()`` cannot disagree about
# which home is which.

#: The cluster plugin surface HSCC owns. Only ``<main>`` (and the operator/
#: gateway root home, D-B) may load all three.
CLUSTER_PLUGINS = ("hscc-cluster", "hscc-commands", "sparkrun-hermes")

#: The hooks-only tier (D-C): every OTHER home still loads ``hscc-cluster``,
#: because 6 of 6 of its hooks are worker/dispatcher-lane
#: (audit §3.2) — ``post_api_request``/``transform_tool_result`` are the
#: ``[budget]`` note the worker SOUL promises, ``pre_tool_call`` stamps the
#: per-profile tool ledger, and ``kanban_task_completed``/``_blocked`` fire
#: inside the worker's own ``complete_task``/``block_task``. Disabling the
#: plugin per-profile silently kills those lanes. ``hscc-commands`` and
#: ``sparkrun-hermes`` register NO hooks, so they do not belong here.
HOOKS_ONLY_PLUGINS = ("hscc-cluster",)


#: Hermes' own sentinel for "this home is the root home, not a profile"
#: (``hermes_cli/plugins.py:400-407`` — ``profile_name`` is ``"default"`` for
#: ``~/.hermes``, the ``<name>`` id for ``~/.hermes/profiles/<name>``). Not the
#: main-orchestrator name, so naming it here does not breach D-A.
ROOT_PROFILE_NAME = "default"


def hermes_home(env=None) -> str:
    """``$HERMES_HOME`` (or ``~/.hermes``), expanded. Read per call."""
    env = os.environ if env is None else env
    return os.path.expanduser((env.get("HERMES_HOME") or "").strip()
                              or os.path.join(os.path.expanduser("~"), ".hermes"))


def hermes_root(home=None, env=None) -> str:
    """The ROOT home for a possibly profile-scoped ``home``.

    ``<root>/profiles/<name>`` is Hermes' normal run mode (the kanban
    dispatcher injects it into every worker, audit §2), so the root is the
    profiles-grandparent. Same rule as ``backup_util._hermes_root`` /
    ``hscc_daemon.hermes_python.hermes_root``; written here in stdlib so the
    resolver stays importable from a plugin ``register()`` with nothing else on
    the path.
    """
    home = os.path.abspath(os.path.expanduser(str(home if home is not None
                                                   else hermes_home(env))))
    if os.path.basename(os.path.dirname(home)) == "profiles":
        return os.path.dirname(os.path.dirname(home))
    return home


def is_root_home(home=None, env=None) -> bool:
    """True when ``home`` IS the root home (operator CLI + gateway + dispatcher).

    That home is the one Hermes home that is not a profile, and the audit's
    D-B allows the cluster surface there alongside ``<main>``.
    """
    target = os.path.abspath(os.path.expanduser(
        str(home if home is not None else hermes_home(env))))
    return hermes_root(target) == target


def plugins_for_home(*, profile=None, home=None, hscc_dir=None,
                     env=None) -> tuple:
    """The ``plugins.enabled`` tier the home of ``profile`` should carry.

    ``<main>`` and the root home get all of :data:`CLUSTER_PLUGINS`; every
    other home gets :data:`HOOKS_ONLY_PLUGINS` (hooks only — no
    ``hscc-commands``, no ``sparkrun-hermes``).

    ``profile`` is Hermes' active profile name (``ctx.profile_name``; Hermes'
    :data:`ROOT_PROFILE_NAME` sentinel for the root home) and ``home`` is that
    home's directory. Only a name equal to :func:`main_profile` opens the full
    tier — the default name itself is never compared here (D-A).

    A caller that names a ``home`` gets the answer for THAT home: this is the
    config writer, and failing open here would write the wide tier into a
    worker profile — the widening the ruling forbids. Under
    ``<root>/profiles/<name>`` the directory name IS the profile name (Hermes'
    own rule, and the only form a tree-sweeping writer has), so a home with no
    ``profile`` argument is resolved from its path before the tier is chosen.
    Only a caller with nothing resolvable at all (no name, no home, no
    ``HERMES_HOME``) falls back to the full tier.
    """
    name = (profile or "").strip()
    if not name and home is not None:
        h = os.path.abspath(os.path.expanduser(str(home)))
        if os.path.basename(os.path.dirname(h)) == "profiles":
            name = os.path.basename(h)
    if name and name == main_profile(hscc_dir):
        return CLUSTER_PLUGINS
    if name == ROOT_PROFILE_NAME:
        return CLUSTER_PLUGINS
    if home is not None or (os.environ if env is None else env).get("HERMES_HOME"):
        return CLUSTER_PLUGINS if is_root_home(home, env=env) else HOOKS_ONLY_PLUGINS
    if not name:
        return CLUSTER_PLUGINS          # nothing resolvable — see docstring
    return HOOKS_ONLY_PLUGINS           # a named non-main profile


def cluster_surface_allowed(*, profile=None, home=None, hscc_dir=None,
                            env=None) -> bool:
    """May this home register the cluster TOOLS/COMMANDS (vs. hooks only)?

    D-B's gate: tools register only in ``<main>`` or in the root home that hosts
    the operator's interactive CLI, the gateway and the kanban dispatcher;
    hooks register everywhere (see :data:`HOOKS_ONLY_PLUGINS`).

    Fail-open on an unresolvable caller: with no profile name and no home
    directory (a stub ctx in a test, a catalog pass that never sets
    ``HERMES_HOME``) the answer is "allowed". Stripping the surface is the
    dangerous direction — it silences the operator's wedge path and, because
    the gateway multiplexes per-profile sessions from the root home, it can
    strip the orchestrator itself — whereas widening can only ever follow an
    explicitly resolvable non-main profile, which is what the ruling closes.
    """
    name = (profile or "").strip()
    if name == main_profile(hscc_dir):
        return True
    if name == ROOT_PROFILE_NAME:
        return True
    if not name:
        # No name: judge by directory, and fail open when there is none.
        if home is None and not (os.environ if env is None else env).get("HERMES_HOME"):
            return True
        return is_root_home(home, env=env)
    if home is None:
        # A named non-main profile with no directory to contradict it.
        return False
    return is_root_home(home, env=env)


def plugin_surface_allowed(ctx, *, hscc_dir=None, env=None) -> bool:
    """``register()``-side wrapper of :func:`cluster_surface_allowed`.

    Reads the home off the Hermes ``PluginContext`` (``ctx.profile_name``,
    ``hermes_cli/plugins.py:400-407`` — derived from ``HERMES_HOME``, so the
    gateway and kanban workers get it too) and the home directory off the same
    environment Hermes routed the process with.

    A ctx WITHOUT a usable ``profile_name`` is not a Hermes context at all — a
    test stub, or a catalog pass that never set ``HERMES_HOME`` — and is allowed
    WITHOUT consulting the ambient ``HERMES_HOME``. Reading ambient env for a
    stub would let whatever home a test suite happens to export decide whether
    the plugin registers its tools, which is the ambient-state bug class this
    fleet has been bitten by before.
    """
    name = getattr(ctx, "profile_name", None)
    if not isinstance(name, str) or not name.strip():
        return True
    env = os.environ if env is None else env
    return cluster_surface_allowed(profile=name, home=env.get("HERMES_HOME"),
                                   hscc_dir=hscc_dir, env=env)


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
