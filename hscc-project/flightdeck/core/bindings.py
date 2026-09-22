"""bindings.py — the canonical session→project binding store.

ONE store holds every ``session_id -> {project, method, confidence, evidence}``
binding, regardless of who wrote it: ``map_sessions --apply`` merges its
resolved proposals here, ``hscc project link`` / ``unlink`` mutate it, and
session discovery (:mod:`flightdeck.core.session_discovery`) reads it. There is
no second store.

The file is the plain JSON mapping under the SAME proposals dir map_sessions
writes to:

    ~/.hermes/archive/telegram/proposals/mapping.json

Schema (one object member per bound session)::

    {
      "<session_id>": {
        "project":   "<project name>",
        "method":    "manual" | "repo-path" | "model" | "none",
        "confidence": 1.0,            # a NUMBER, never a string
        "evidence":  "human text naming who decided and why"
      }
    }

Linking is METADATA, never a physical merge of sessions (no tool_call
adjacency, no compaction-header hijack). Reversible at any time by editing or
deleting the file.

Every operation is FILE-ONLY: it touches ONLY ``mapping.json``, never
``state.db``, the registry, or a kanban board.

The path is a module constant (:data:`MAPPING_FILE`) that is injectable for
tests — every function takes an optional ``mapping_path`` override.
"""

from __future__ import annotations

import getpass
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# The single canonical binding-store file (orchestrator-settled), shared by
# link/unlink, `map-sessions --apply`, and session discovery. Lives under the
# SAME proposals dir map_sessions writes to.
MAPPING_FILE = "~/.hermes/archive/telegram/proposals/mapping.json"

# The method tag manual link/unlink writes. Distinct from the proposal methods
# (repo-path / model / none) so the source of a binding is always clear.
METHOD_MANUAL = "manual"


def _resolve(mapping_path: str | None) -> Path:
    return Path(os.path.expanduser(mapping_path or MAPPING_FILE))


def load(mapping_path: str | None = None) -> dict:
    """The whole binding store ``{session_id: {...}}``, or ``{}``.

    A missing file, an unreadable file, or a file that is not a JSON object
    yields no bindings (``{}``) — a missing or malformed mapping is a data
    fact, never an environment fault, so it fails SOFT rather than raising.
    ``mapping_path`` defaults to :data:`MAPPING_FILE` and is injectable for
    tests to point at a temp file.
    """
    path = _resolve(mapping_path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def save(data: dict, mapping_path: str | None = None) -> None:
    """Atomically write the whole store to ``mapping.json`` (valid JSON, always).

    Writes to a temp file in the same directory then ``os.replace`` over the
    target, so a crash mid-write can never leave a truncated/corrupt
    ``mapping.json`` — the reader either sees the old complete file or the new
    complete file, never a torn one. Creates parent directories as needed.
    """
    path = _resolve(mapping_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=".mapping-", suffix=".json.tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _evidence_text(operator: str | None, date: str | None) -> str:
    """``manual link by <operator> on <date>`` — the default evidence string."""
    op = operator or getpass.getuser() or "operator"
    day = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"manual link by {op} on {day}"


def bind(
    session_id: str,
    project: str,
    *,
    method: str = METHOD_MANUAL,
    confidence: float = 1.0,
    evidence: str | None = None,
    mapping_path: str | None = None,
) -> dict:
    """Insert or replace ``session_id``'s binding to ``project``.

    IDEMPOTENT: linking the same session to the same project again yields the
    same result. Linking to a DIFFERENT project overwrites the binding
    (last-write-wins is fine for manual). ``confidence`` stays NUMERIC
    (default ``1.0``). Returns the new entry for the caller to echo.
    """
    store = load(mapping_path)
    entry = {
        "project": project,
        "method": method,
        "confidence": float(confidence),
        "evidence": evidence if evidence is not None
        else _evidence_text(None, None),
    }
    store[session_id] = entry
    save(store, mapping_path)
    return entry


def unbind(session_id: str, mapping_path: str | None = None) -> bool:
    """Remove ``session_id`` from the store entirely.

    ``True`` when an entry was removed, ``False`` when there was none (the
    caller says so rather than erroring). Never errors on a missing binding or
    a missing file.
    """
    store = load(mapping_path)
    if session_id not in store:
        return False
    del store[session_id]
    save(store, mapping_path)
    return True


def list(mapping_path: str | None = None, project: str | None = None) -> dict:
    """All bindings, or just ``project``'s.

    With ``project`` set, returns only the entries whose stored ``project``
    equals it. Always a dict (possibly empty); never mutates the file.
    """
    store = load(mapping_path)
    if project is None:
        return store
    return {sid: meta for sid, meta in store.items()
            if isinstance(meta, dict) and meta.get("project") == project}
