"""Which directories under ``~/.hermes/profiles`` are REAL profiles.

``hscc verify``'s multiplex check and the daemon's gateway stream both compare
the on-disk profile roster against the roster the gateway recorded in
``served_profiles``. The gateway builds ``served_profiles`` from Hermes' own
enumeration (:func:`hermes_cli.profiles.profiles_to_serve` ->
:func:`hermes_cli.profiles._iter_named_profile_dirs`), which counts a directory
as a profile only when it carries a profile identity marker and is not
tombstoned. Any HSCC-side comparison that instead takes "every directory under
``profiles/``" therefore demands the gateway serve directories Hermes refuses
to recognise as profiles — a permanently unachievable requirement, and the
false-fail this module exists to prevent.

Two such scanners existed (t_1c545ebb): ``verify.check_multiplex`` treated
``profiles/.deleted`` — Hermes' tombstone directory — as a profile id, and both
it and ``health._check_multiplex_profiles`` counted ``profiles/ecofire-bc``
(a leftover ``workspaces/`` stub with no profile config) as expected-served.
They also disagreed with each other: the health scanner skipped dot-dirs, the
verify scanner did not, so ``gateway.json`` said "1 profile(s) not served"
while ``hscc verify`` said two.

Hermes' predicate is used when importable, because it is the authority the
gateway is built on and stays in sync as Hermes evolves. It is NOT required:
the daemon ships under ``~/.hermes/plugins`` and ``hermes_constants`` is not
guaranteed to be on its ``sys.path``, so a self-contained mirror of the same
rule is the fallback. The fallback is deliberately conservative in the
fail-CLOSED direction — a directory counts as a profile unless it is clearly
not one (dot-dir, bad id grammar, no identity marker, tombstoned) — so a
partial or missing Hermes install can never make a genuinely unserved profile
look served.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

__all__ = ["list_profiles_on_disk", "DELETED_PROFILES_DIRNAME"]

#: Directory Hermes moves deleted profiles' tombstones into
#: (``hermes_constants._DELETED_PROFILES_DIR``). Never a profile id.
DELETED_PROFILES_DIRNAME = ".deleted"

#: A directory under ``profiles/`` counts as a profile only when it carries one
#: of these — mirrors ``hermes_constants._PROFILE_IDENTITY_MARKERS``. Runtime
#: side-effects (cron heartbeats, log rotation, caches, an orphaned
#: ``workspaces/`` tree) create marker-less dirs that are not profiles.
_IDENTITY_MARKERS = ("config.yaml", ".env", "SOUL.md", "profile.yaml",
                     "auth.json", "state.db")

#: Canonical named-profile id grammar — mirrors ``hermes_constants.PROFILE_ID_RE``.
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

#: ``gateway.config._TRUTHY_STRINGS`` / ``_FALSY_STRINGS`` — Hermes' own bool
#: tokens for ``gateway.standalone``. Kept here so the mirror branch reads that
#: key the same way the gateway does instead of inventing its own truthiness.
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})


def _identity_markers(home: str) -> bool:
    """Fallback marker test: does ``home`` claim a profile identity?

    Symlinked markers count (a dangling symlink is still an identity claim), as
    in Hermes.
    """
    for marker in _IDENTITY_MARKERS:
        path = os.path.join(home, marker)
        try:
            if os.path.isfile(path) or os.path.islink(path):
                return True
        except OSError:
            continue
    return False


def _mirror_is_standalone(home: str) -> bool:
    """Fallback ``gateway.standalone: true`` reader (see ``_hermes_gate``).

    Only the ``gateway:`` mapping's ``standalone`` key counts — there is no
    top-level alias in Hermes. Unreadable/absent config or a missing yaml
    reader returns False: the directory then stays on the expected roster, which
    is the pre-existing behaviour for that (rare) case rather than a new
    exemption.
    """
    cfg_path = os.path.join(home, "config.yaml")
    try:
        if not os.path.isfile(cfg_path):
            return False
        try:
            import yaml
        except ImportError:
            return False
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
    except OSError:
        return False
    if not isinstance(cfg, dict):
        return False
    gateway = cfg.get("gateway")
    if not isinstance(gateway, dict):
        return False
    value = gateway.get("standalone")
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _TRUTHY:
            return True
        if token in _FALSY:
            return False
        return False
    return bool(value)


def _hermes_gate():
    """Return Hermes' ``(grammar, identity, deleted, standalone)`` predicates.

    ``hermes_cli`` lives under ``~/.hermes/hermes-agent`` and is never on the
    plugin's ``sys.path``, so this mirrors the sanctioned lazy-import pattern
    used for ``hermes_cli.kanban_db`` (``autodown._load_kanban_db_or_default``):
    resolve the runtime dir, extend ``sys.path`` only for the import, and
    return None on any failure so the caller falls back to the mirror above.
    """
    import sys

    def _load():
        from hermes_constants import (PROFILE_ID_RE, named_profile_has_identity,
                                      named_profile_is_deleted)
        from hermes_cli.profiles import profile_is_standalone
        return (PROFILE_ID_RE, named_profile_has_identity,
                named_profile_is_deleted, profile_is_standalone)

    try:
        return _load()
    except ImportError:
        pass
    hermes_path = os.path.expanduser(
        os.environ.get("HERMES_AGENT_PATH", "~/.hermes/hermes-agent"))
    if not os.path.isdir(hermes_path) or hermes_path in sys.path:
        return None
    sys.path.insert(0, hermes_path)
    try:
        return _load()
    except ImportError:
        return None


def _tombstoned(profiles_dir: str, name: str) -> bool:
    """Hermes' tombstone layout: ``profiles/.deleted/<name>`` marks a deleted
    profile whose directory is still on disk."""
    return os.path.exists(os.path.join(profiles_dir, DELETED_PROFILES_DIRNAME,
                                      name))


def list_profiles_on_disk(profiles_dir: str) -> list[str]:
    """Sorted names of the real profiles under ``profiles_dir``.

    Hermes' rule in full — anything it would not serve is not expected either:
      * dot-directories and names outside the profile-id grammar are not
        profile ids (``.deleted`` is Hermes' tombstone DIRECTORY, not a profile);
      * a directory must carry one of Hermes' identity markers
        (``config.yaml``, ``.env``, ``SOUL.md``, ``profile.yaml``,
        ``auth.json``, ``state.db``) — a leftover holding only ``workspaces/``
        is not a profile and the gateway will never serve it;
      * tombstoned (deleted-but-still-on-disk) and parked profiles are skipped,
        as are profiles setting ``gateway.standalone: true``, which opted out of
        the host multiplexer exactly as ``profiles_to_serve`` does.

    ``default`` is never returned: on some hosts it exists as a directory under
    ``profiles/``, but Hermes treats it as the root home (always served when
    multiplex is on), so counting it could only ever produce a false gap.

    Returns ``[]`` when ``profiles_dir`` is missing or unreadable — callers
    that report a verdict distinguish "no profiles" from "cannot tell" by
    testing ``os.path.isdir`` themselves.
    """
    profiles_dir = os.path.expanduser(profiles_dir)
    if not os.path.isdir(profiles_dir):
        return []

    gate = _hermes_gate()
    names: list[str] = []
    try:
        entries = sorted(os.scandir(profiles_dir), key=lambda e: e.name)
    except OSError:
        return []

    for entry in entries:
        name = entry.name
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        if name == "default":
            continue
        if gate is not None:
            id_re, has_identity, is_deleted, is_standalone = gate
            if name.startswith(".") or not id_re.match(name):
                continue
            if not has_identity(entry.path):
                continue
            if is_deleted(entry.path):
                continue
            try:
                if is_standalone(Path(entry.path)):
                    continue
            except Exception:
                # Cannot prove the opt-out -> keep it on the expected roster
                # (fail-CLOSED: never invent an exemption).
                pass
        else:
            if name.startswith(".") or not _ID_RE.match(name):
                continue
            if not _identity_markers(entry.path):
                continue
            if _tombstoned(profiles_dir, name):
                continue
            if _mirror_is_standalone(entry.path):
                continue
        # Parked profiles are skipped by the gateway too (hermes
        # `profile_is_parked`); a marker file is the only signal.
        if os.path.exists(os.path.join(entry.path, "gateway.parked")):
            continue
        names.append(name)
    return names
