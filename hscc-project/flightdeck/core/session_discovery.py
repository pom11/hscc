"""session_discovery.py — surface a project's Telegram session history.

A project's REAL history lives on the DEFAULT profile, in Telegram-originated
sessions. This module makes that history DISCOVERABLE and RESUMABLE from hscc:

  * ``list_project_sessions`` — every session belonging to a project: the
    permanent CLI orchestrator session AND the Telegram ones, newest first.

The mapping is the registry ``topic`` -> ``sessions.thread_id`` on the DEFAULT
profile, ``source='telegram'``. All reads go through Hermes' OWN ``SessionDB``
API (``list_sessions_rich`` / ``get_session``), opened READ-ONLY — the
discovery command never writes to the operator's live session databases and
never hand-rolls SQL.

Both DB opens use ``SessionDB(..., read_only=True)`` — Hermes' native
equivalent of ``mode=ro`` on the underlying SQLite connection. That is the
hermes-API way to satisfy the read-only constraint (no raw ``PRAGMA`` /
``connect(mode=ro)`` in our code). The ``thread_id == topic`` filter is applied
in Python on the returned rows because ``list_sessions_rich`` filters by
source/session_key/cwd but has no ``thread_id`` parameter.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import registry
from .project_lifecycle import _open_profile_session_db as _open_profile_sdb_ro
from . import project_lifecycle as _lifecycle


# The project -> orchestrator identity resolver lives in hscc-roles/
# orchestrators.py (vendored verbatim; the REST chat handler in
# hscc-api/routes_orchestrator.py loads it the exact same way). We load it
# under the same sys.path pattern so `hscc project sessions <name>` and the
# chat note agree with `resolve_orchestrator` on the {profile, session}.
_ROLES_DIR = Path(__file__).resolve().parent.parent.parent.parent / "hscc-roles"
if _ROLES_DIR.is_dir() and str(_ROLES_DIR) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(_ROLES_DIR))

from orchestrators import (  # noqa: E402
    OrchestratorError,
    UnknownProjectError,
    resolve_orchestrator,
)

# Telegram sessions live on the DEFAULT profile, not the <name>-orch one.
DEFAULT_PROFILE = "default"

# The single canonical binding store (orchestrator-settled): session_id ->
# {project, method, confidence, evidence}, written by `map_sessions --apply`
# (card 2 makes it the canonical merge target; today it may be absent — then
# there are simply no bindings and discovery behaves exactly as before). The
# file lives under the SAME proposals dir map_sessions writes to.
MAPPING_FILE = "~/.hermes/archive/telegram/proposals/mapping.json"


def _load_binding_store(mapping_path: str | None = None) -> dict:
    """``session_id -> {project, method, confidence, evidence}``, or ``{}``.

    Read-only on the plain binding-store file: no file, or a file that is
    unreadable / not a JSON object, yields no bindings (discovery then behaves
    exactly as before this card — a missing or malformed mapping is a data
    fact, never an environment fault, so it fails soft rather than raising).
    ``mapping_path`` defaults to the module :data:`MAPPING_FILE` and is
    injectable for tests to point at a temp ``mapping.json``.
    """
    if mapping_path is None:
        mapping_path = MAPPING_FILE
    path = Path(os.path.expanduser(mapping_path))
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _open_profile_session_db(profile: str):
    """Open ``<profile>``'s state.db READ-ONLY via Hermes' own SessionDB.

    Thin wrapper over ``project_lifecycle._open_profile_session_db`` with
    ``read_only=True`` forced — discovery never writes to the operator's live
    session databases (none are touched read-write anywhere in this module).
    ``None`` when the profile is unresolvable or has no state.db (the caller
    then reports an honest "no profile / no db" line rather than guessing).
    """
    return _open_profile_sdb_ro(profile, read_only=True)


def _runtime_error() -> str | None:
    """Return a message when the Hermes runtime is unimportable, else None.

    Callers use this to distinguish "no history" from "wrong interpreter".
    """
    try:
        _open_profile_sdb_ro("default", read_only=True)
    except _lifecycle.HermesRuntimeUnavailable as exc:
        return str(exc)
    except Exception:
        return None
    return None


def _session_row_summary(row: dict, source: str) -> dict:
    """Project a hermes `sessions` row dict onto the discovery shape.

    Uses the stable user-facing name (``title``) and the chain-tip-aware
    activity time (``last_active`` where present, else the newest available
    activity stamp). Every field falls back safely: counts/timestamps may be
    missing on a fresh or partially-written session.
    """
    title = row.get("title") or row.get("display_name") or row.get("id")
    last = (
        row.get("last_active")
        or row.get("last_activity_at")
        or row.get("ended_at")
        or row.get("started_at")
    )
    return {
        "id": row.get("id"),
        "title": title,
        "message_count": row.get("message_count") or 0,
        "first": row.get("started_at"),
        "last": last,
        "source": source,
    }


def _load_project_topic(name: str, path: str | None) -> int | None:
    """The registry ``topic`` for ``name``, or None when unbindable/unknown.

    The topic is the Telegram thread id that identifies (potentially many)
    telegram sessions as belonging to this project. A project with no topic
    cannot be mapped to any telegram history.
    """
    for proj in registry.load_registry(path):
        if proj.name == name:
            return proj.topic
    return None


def _list_telegram_sessions(_default_db=None) -> list[dict]:
    """Telegram sessions on the DEFAULT profile, via Hermes' own read-only API.

    Opens the DEFAULT profile's state.db through ``hermes_state.SessionDB`` in
    READ-ONLY mode (the Hermes-native ``mode=ro``) and lists sessions with
    ``list_sessions_rich(source='telegram')`` — the same call the ``hermes
    sessions --source telegram`` CLI and the chat relay use. Fail-soft: no
    readable default state.db returns ``[]`` (the note is simply omitted,
    never an error).
    """
    opener = _default_db if _default_db is not None else _open_profile_session_db
    db = opener(DEFAULT_PROFILE)
    if db is None:
        return []
    try:
        return db.list_sessions_rich(source="telegram", limit=5000)
    except Exception:
        return []
    finally:
        try:
            db.close()
        except Exception:
            pass


def list_project_sessions(
    name: str, path: str | None = None, _session_db=None, _default_db=None,
    _mapping_path: str | None = None,
) -> dict:
    """Every session belonging to project ``name``, newest first.

    Reads three facts through Hermes' own APIs, all READ-ONLY:

      * the orchestrator identity (``resolve_orchestrator`` from hscc-roles,
        the SAME resolver the CLI chat, the REST handler and the WS relay use);
      * the orchestrator session's own row (title / message count / activity)
        from its ``<name>-orch`` profile's state.db;
      * every Telegram session on the DEFAULT profile that BELONGS to the
        project — a session belongs if its ``thread_id`` equals the registry
        ``topic`` OR the binding store explicitly maps its ``session_id`` to
        this project (the ``mapping.json`` file; see :data:`MAPPING_FILE`).

    Returns a single dict::

        {
          "project": name,
          "topic":  <int | None>,     # registry topic (telegram thread id)
          "orchestrator": <dict | None>,  # CLI/permanent orchestrator session
          "telegram": [ <row>, ... ],     # thread_id == topic OR linked via
                                          # binding store, newest first
          "telegram_unmapped": <int>,     # default telegram sessions with
                                          # thread_id NULL — cannot own them
          "orch_profile": "<name>-orch",
          "telegram_profile": "default",
        }

    ``thread_id`` comes back from hermes as a string, so it is compared to the
    registry int ``topic`` after normalisation. Telegram sessions with
    ``thread_id = None`` cannot be mapped to any project by thread — they are
    counted in ``telegram_unmapped``, never guessed at. A session is mapped by
    ``thread_id == topic`` (rule 1) OR by an explicit ``session_id -> project``
    entry in the binding store (rule 2, unioned and de-duplicated).

    ``_mapping_path`` overrides the binding-store file for tests (defaults to
    the module :data:`MAPPING_FILE`; an absent/empty store means \"no
    bindings\"). The command wiring never passes it, so the real canonical
    file is used in production.
    """
    resolved = resolve_orchestrator(name, path=path)
    orch_profile = resolved["profile"]
    orch_session = resolved["session"]
    topic = _load_project_topic(name, path)

    # Bail out with an explicit reason rather than empty lists when this
    # interpreter cannot see the Hermes runtime at all.
    _rt = _runtime_error()
    if _rt is not None:
        return {
            "project": name,
            "topic": topic,
            "orchestrator": None,
            "telegram": [],
            "telegram_unmapped": 0,
            "orch_profile": orch_profile,
            "telegram_profile": DEFAULT_PROFILE,
            "runtime_error": _rt,
        }

    # Orchestrator session row (read-only from its own profile's db).
    orch_row = None
    orch_provider = (
        _session_db if _session_db is not None else _open_profile_session_db
    )
    orch_db = orch_provider(orch_profile)
    if orch_db is not None:
        try:
            row = orch_db.get_session(orch_session)
        except Exception:
            row = None
        finally:
            try:
                orch_db.close()
            except Exception:
                pass
        if row:
            orch_row = _session_row_summary(row, "orchestrator")

    # Telegram sessions (rule 1: thread_id == topic on the DEFAULT profile;
    # rule 2: session_id explicitly linked to this project in the binding
    # store). The two rules are UNIONED and de-duplicated — a session that
    # matches both appears once.
    telegram_rows = _list_telegram_sessions(_default_db=_default_db)
    unmapped = sum(1 for r in telegram_rows if r.get("thread_id") is None)
    mined: list[dict] = []
    thread_matched: set = set()

    if topic is not None:
        for r in telegram_rows:
            tid = r.get("thread_id")
            if tid is None:
                continue
            try:
                matches = int(tid) == topic
            except (TypeError, ValueError):
                matches = False
            if matches:
                sid = r.get("id")
                if sid is not None:
                    thread_matched.add(sid)
                mined.append(_session_row_summary(r, "telegram"))

    # Binding store: a session whose stored `project` equals `name` belongs
    # even when its thread_id doesn't match the topic (manual linking).
    bindings = _load_binding_store(_mapping_path)
    bound_ids = {
        sid for sid, meta in bindings.items()
        if isinstance(meta, dict) and meta.get("project") == name
    }
    if bound_ids:
        for r in telegram_rows:
            sid = r.get("id")
            if sid in bound_ids and sid not in thread_matched:
                mined.append(_session_row_summary(r, "telegram"))

    mined.sort(
        key=lambda s: (
            -1 if s["last"] is None else s["last"],  # newest first, null last
            s.get("first") or 0,                     # stable tiebreak
            s.get("id") or "",                       # fully deterministic
        ),
        reverse=True,
    )

    return {
        "project": name,
        "topic": topic,
        "orchestrator": orch_row,
        "telegram": mined,
        "telegram_unmapped": unmapped,
        "orch_profile": orch_profile,
        "telegram_profile": DEFAULT_PROFILE,
        # Non-None when this interpreter cannot import the Hermes runtime, in
        # which case the empty lists above mean "could not look", NOT "nothing
        # there". The caller must say which.
        "runtime_error": _runtime_error(),
    }
