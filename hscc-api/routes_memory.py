"""HSCC HTTP API — memory viewer (read / correct / delete a profile's memories).

The operator's view into what a Hermes profile "remembers" — the curated
entries the agent's ``memory`` tool keeps in ``~/.hermes/profiles/<name>/
memories/MEMORY.md`` (personal notes, source ``memory``) and ``USER.md``
(user profile, source ``profile``). This is the card's memory viewer: list
every memory a profile holds, and correct or delete one that is wrong.

Contract:

  * ``GET /v1/memory?profile=<name>`` — the profile's memory cards, in the
    exact order/shape the Hermes journey graph renders them (all ``MEMORY.md``
    cards first, then ``USER.md``), each with its stable graph node id so the
    operator can pass it straight back to delete/edit. Read-only.
  * ``POST /v1/memory/{node_id}/delete`` (confirm-gated) — delete one memory
    card. Requires ``profile`` and ``confirm: true`` in the body.
  * ``POST /v1/memory/{node_id}/edit`` (confirm-gated) — correct one memory
    card by replacing its content. Requires ``profile``, ``content`` and
    ``confirm: true`` in the body.

Design notes:

  * Reads target the RIGHT profile's memories directory directly (resolved via
    ``routes_profile._hermes_profiles_dir``), never the API process's own
    default profile — the same discipline ``routes_sessions`` applies to a
    profile's ``state.db``.
  * The on-disk format is a ``\\n§\\n``-delimited list of entries (the exact
    ``MemoryStore`` format). We parse/serialize it with our own tiny mirror so
    the API is self-contained and hermetic-testable — same delimiter, same
    atomic-write guarantee, same blank-entry dropping. Indices are therefore
    identical to what ``hermes memory-graph``/the journey graph would show for
    that profile, so a node id fetched here maps cleanly back to that CLI.
  * Node ids are ``memory:<source>:<global_index>`` where ``global_index`` is
    the position in the COMBINED card list (MEMORY.md cards first, then
    USER.md). For source ``memory`` local == global; for ``profile`` local =
    global − memory_count. Mirrors ``learning_graph._memory_cards`` +
    ``learning_mutations._memory_local_index``.
  * Following the API's mutation convention (A4 / routes_ops / routes_sessions),
    every mutating endpoint requires ``confirm: true`` (409 ``confirm_required``
    otherwise). Delete/Edit go through the app's MutationButton gate.
  * ``edit`` with an empty ``content`` is refused — removing a memory is
    ``delete``'s job.
"""

from __future__ import annotations

import os
import re
import tempfile
import time
from pathlib import Path

from api_server import ApiError, ROUTES
import routes_profile as _profiles

# --------------------------------------------------------------------------- #
# On-disk format mirror (kept in lockstep with tools/memory_tool.py)
# --------------------------------------------------------------------------- #

# The exact delimiter Hermes' MemoryStore uses to join/separate entries. Kept a
# literal here (not imported from the hermes package) so the API is self-
# contained and hermetic-testable; the byte-for-byte contract is what matters,
# and it must not drift from the tool. Same value as tools/memory_tool.py.
_ENTRY_DELIMITER = "\n§\n"

# Graph source slug -> memory file name (mirrors learning_mutations._MEMORY_FILES).
_MEMORY_FILES = {"memory": "MEMORY.md", "profile": "USER.md"}

# Node ids carry a ":memory:/:profile:" plus a combined index.
_NODE_RE = re.compile(r"^memory:(memory|profile):(\d+)$")


def _memory_dir(profile: str) -> Path | None:
    """Return the profile's memories dir, or None if the profile has none.

    A profile that doesn't exist (or has no memories dir) yields None rather
    than raising, so the read path degrades to an honest empty list instead of
    leaking a filesystem error. Mirrors ``_hermes_profiles_dir`` resolution.
    """
    d = _profiles._hermes_profiles_dir() / profile / "memories"
    return d if d.is_dir() else None


def _parse_entries(raw: str) -> list[str]:
    """Split raw memory-file text into stripped, non-empty entries."""
    if not raw.strip():
        return []
    return [e.strip() for e in raw.split(_ENTRY_DELIMITER) if e]


def _read_entries(path: Path) -> list[str]:
    """Read + parse one memory file (empty list when missing/unreadable)."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return _parse_entries(raw)


def _write_entries(path: Path, entries: list[str]) -> None:
    """Persist entries with an atomic temp-file + rename (never a partial file).

    Mirrors ``MemoryStore._write_file`` (itself atomic via ``os.replace``) so a
    concurrent reader — the running agent, another HTTP request, the operator's
    ``hermes memory-graph`` — never sees a half-written memory file.
    """
    content = _ENTRY_DELIMITER.join(entries) if entries else ""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".mem_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _to_int_ts(value: float) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


# --------------------------------------------------------------------------- #
# Backing seam (monkeypatch in tests)
# --------------------------------------------------------------------------- #

def _backing_cards(profile: str) -> list[dict] | None:
    """Build the profile's memory cards (graph-ordered) or None if no store.

    Returns None — not ``[]`` — when the profile has no memories dir at all, so
    the handler can distinguish "profile unreachable" from "profile holds no
    memories". Both map to different speak text.
    """
    mem_dir = _memory_dir(profile)
    if mem_dir is None:
        return None
    cards: list[dict] = []
    global_idx = 0
    for fname, source in (("MEMORY.md", "memory"), ("USER.md", "profile")):
        path = mem_dir / fname
        file_ts = None
        try:
            file_ts = _to_int_ts(path.stat().st_mtime)
        except OSError:
            pass
        for chunk_idx, chunk in enumerate(_read_entries(path)):
            first = chunk.splitlines()[0].strip().lstrip("# ").strip()
            title = first if len(first) <= 80 else first[:80] + "…"
            cards.append({
                "id": f"memory:{source}:{global_idx}",
                "node_id": f"memory:{source}:{global_idx}",
                "source": source,
                "kind": "memory",
                "timestamp": file_ts + chunk_idx if file_ts is not None else None,
                "title": title,
                "body": chunk,
            })
            global_idx += 1
    return cards


def _locate(profile: str, node_id: str, cards: list[dict] | None,
            read_only: bool = False):
    """Resolve a ``memory:<source>:<gidx>`` node id to its file + entries + local index.

    Returns ``(path, entries, local_index)``. Raises ApiError when the id is
    malformed, the profile has no store, or the id is stale. The caller passes
    ``cards`` (the fresh list from ``_backing_cards``) so the global→local
    mapping never guesses.
    """
    m = _NODE_RE.match(node_id)
    if not m:
        raise ApiError(400, "bad_request",
                       f"'{node_id}' is not a memory node id "
                       f"(expected memory:<memory|profile>:<index>)")
    source, gidx = m.group(1), int(m.group(2))

    if cards is None:
        raise ApiError(404, "profile_unreachable",
                       f"profile '{profile}' has no memory store",
                       "That profile has no memories.")
    if not 0 <= gidx < len(cards):
        raise ApiError(404, "not_found",
                       f"memory node '{node_id}' not found — refresh the list",
                       "That memory no longer exists.")
    if cards[gidx]["source"] != source:
        raise ApiError(404, "not_found",
                       f"memory node '{node_id}' is stale — refresh the list",
                       "That memory no longer exists.")

    mem_dir = _memory_dir(profile)
    if mem_dir is None:
        raise ApiError(404, "profile_unreachable",
                       f"profile '{profile}' has no memory store",
                       "That profile has no memories.")
    path = mem_dir / _MEMORY_FILES[source]
    if not path.exists():
        raise ApiError(404, "profile_unreachable",
                       f"profile '{profile}' has no memory store",
                       "That profile has no memories.")
    entries = _read_entries(path)
    if source == "memory":
        local = gidx
    else:
        local = gidx - sum(1 for c in cards if c.get("source") == "memory")
    if not 0 <= local < len(entries):
        raise ApiError(404, "not_found",
                       f"memory node '{node_id}' is stale — refresh the list",
                       "That memory no longer exists.")
    return path, entries, local


# --------------------------------------------------------------------------- #
# Body helpers (confirm gate mirrors routes_sessions / routes_ops)
# --------------------------------------------------------------------------- #

def _parse_body(body: bytes) -> dict:
    import json
    if not body:
        return {}
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ApiError(400, "bad_request", "request body must be JSON")
    if not isinstance(data, dict):
        raise ApiError(400, "bad_request", "request body must be a JSON object")
    return data


def _require_confirm(data: dict, what: str) -> None:
    if data.get("confirm") is True:
        return
    raise ApiError(
        409, "confirm_required",
        f"this action changes a profile's memories and requires "
        f"\"confirm\": true in the request body to {what}",
        f"Confirmation required to {what}.",
    )


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #

def handle_memory_list(server, ctx, query, body):
    """GET /v1/memory — a profile's memory cards (read-only)."""
    profile = (query.get("profile") or "").strip()
    if not profile:
        raise ApiError(400, "bad_request",
                       "missing required query param 'profile'")
    cards = _backing_cards(profile)
    if cards is None:
        return 200, {"profile": profile, "memories": [], "count": 0,
                     "speak": f"Profile '{profile}' has no memory store."}
    mem = sum(1 for c in cards if c.get("source") == "memory")
    prof = len(cards) - mem
    speak = (f"{len(cards)} memor{'y' if len(cards) == 1 else 'ies'} for "
             f"{profile} ({mem} notes, {prof} profile).")
    return 200, {
        "profile": profile,
        "memories": cards,
        "count": len(cards),
        "memory_count": mem,
        "profile_count": prof,
        "speak": speak,
    }


def handle_memory_delete(server, ctx, query, body):
    """POST /v1/memory/{node_id}/delete — delete one memory card (confirm-gated)."""
    node_id = query.get("node_id")
    data = _parse_body(body)
    _require_confirm(data, "delete this memory")
    profile = (data.get("profile") or "").strip()
    if not profile:
        raise ApiError(400, "bad_request",
                       "missing required 'profile' in request body")
    cards = _backing_cards(profile)
    if cards is None:
        raise ApiError(404, "profile_unreachable",
                       f"profile '{profile}' has no memory store",
                       "That profile has no memories.")
    path, entries, local = _locate(profile, node_id, cards)
    deleted = entries.pop(local)
    _write_entries(path, entries)
    label = deleted.splitlines()[0].strip().lstrip("# ").strip()[:80]
    return 200, {
        "node_id": node_id,
        "kind": "memory",
        "title": label,
        "message": f"deleted memory from {path.name}",
        "speak": f"Deleted the memory \"{label}\".",
    }


def handle_memory_edit(server, ctx, query, body):
    """POST /v1/memory/{node_id}/edit — correct a memory card (confirm-gated)."""
    node_id = query.get("node_id")
    data = _parse_body(body)
    _require_confirm(data, "edit this memory")
    profile = (data.get("profile") or "").strip()
    if not profile:
        raise ApiError(400, "bad_request",
                       "missing required 'profile' in request body")
    content = (data.get("content") or "").strip()
    if not content:
        raise ApiError(400, "bad_request",
                       "missing required non-empty 'content' in request body — "
                       "use delete to remove a memory")
    cards = _backing_cards(profile)
    if cards is None:
        raise ApiError(404, "profile_unreachable",
                       f"profile '{profile}' has no memory store",
                       "That profile has no memories.")
    path, entries, local = _locate(profile, node_id, cards)
    prev = entries[local].strip()
    entries[local] = content
    _write_entries(path, entries)
    label = prev.splitlines()[0].strip().lstrip("# ").strip()[:80]
    return 200, {
        "node_id": node_id,
        "kind": "memory",
        "previous_title": label,
        "message": f"updated memory in {path.name}",
        "speak": f"Corrected the memory in {path.name}.",
    }


# --------------------------------------------------------------------------- #
# Route registration (import side-effect; loaded by api_server.py)
# --------------------------------------------------------------------------- #

ROUTES.append(("GET", re.compile(r"^/v1/memory$"), handle_memory_list))
ROUTES.append(
    ("POST", re.compile(r"^/v1/memory/(?P<node_id>[^/]+)/delete$"),
     handle_memory_delete)
)
ROUTES.append(
    ("POST", re.compile(r"^/v1/memory/(?P<node_id>[^/]+)/edit$"),
     handle_memory_edit)
)
