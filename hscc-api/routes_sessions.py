"""HSCC HTTP API — sessions manager (list a profile's sessions, retire/compact).

The operator's view into a profile's Hermes sessions (the ``state.db`` each
Hermes profile maintains, the same store ``hermes -p <profile> chat --continue``
reads and writes). This is the card's sessions manager: list every listable
session with its message count, token totals and compaction headroom, and
retire or compact one that is bloated.

Contract:

  * ``GET /v1/sessions?profile=<name>`` — the profile's listable, non-archived
    sessions, newest first. Each row carries the session's real numbers
    (message count, token totals across every accounted-for stream) plus the
    compaction signals and an honest verdict/headroom derived from the SAME
    machinery the orchestrator bloat-guard uses — no re-implementation, no
    guessing.
  * ``POST /v1/sessions/{session_id}/retire`` (confirm-gated) — non-destructive
    retirement of one bloated session by id: retitled ``<title>-retired-<ts>``
    (kept on disk with all messages intact, dropped out of the live listable
    set). Requires ``profile`` in the body.
  * ``POST /v1/sessions/{session_id}/compact`` (confirm-gated) — re-arm native
    compaction on one session by id: clears the compaction-failure latch so
    Hermes' real compressor retakes the floor on its next turn and shrinks the
    session (full history preserved via the summary — no deletion). Requires
    ``profile`` in the body.

Design notes:

  * Session source-of-truth is ``hermes_state.SessionDB`` via the exact same
    ``routes_orchestrator._open_profile_session_db`` helper the bloat-guard
    uses — reads target the RIGHT profile's state.db, never the API process's
    own default profile. The read path opens it read-only (no write lock,
    so a busy profile's DB is never contended for a status list); mutations
    open it read-write.
  * A session is never judged "bloated" merely for being LARGE —
    ``input_tokens`` is a CUMULATIVE counter (never reset by compaction), so
    big does not mean unhealthy. Bloat is decided solely on POSITIVE
    compaction-failure evidence via ``_session_bloat_verdict`` (the same rule
    the orchestrator rotation uses). ``compaction_headroom`` = ``context_window``
    − ``threshold_tokens``: the stable, guaranteed room for the compression
    call that the early-threshold ensure provides.
  * ``compact`` is distinct from ``retire``. Retire discards the session from
    the live set (continuity broken, last resort). Compact KEEPS the session and
    continuity: it only resets the failure latch + re-ensures the early cap, so
    the next turn's native compaction actually shrinks it. Both are real,
    working actions — neither is a stubbed success.
  * Following the API's mutation convention (routes_actions A4 /
    routes_ops), every mutating endpoint requires ``confirm: true`` (409
    ``confirm_required`` otherwise).
"""

from __future__ import annotations

import re

from api_server import ApiError, ROUTES
import routes_orchestrator as _orch


# --------------------------------------------------------------------------- #
# Backing seam (monkeypatch in tests): open a profile's session DB
# --------------------------------------------------------------------------- #
# Reuses routes_orchestrator._open_profile_session_db directly — the same
# resolver the bloat-guard uses, so sessions are always read from the right
# profile. Wrapped here only so tests can target this module's surface without
# reaching into the orchestrator module.

def _backing_open_profile_db(profile: str, read_only: bool = False):
    return _orch._open_profile_session_db(profile, read_only=read_only)


def _backing_ensure_threshold(profile: str) -> dict | None:
    """Re-ensure the profile's early compaction cap (idempotent, fail-safe)."""
    return _orch._ensure_compaction_threshold(profile)


# --------------------------------------------------------------------------- #
# Body helpers (confirm gate mirrors routes_ops)
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
        raise ApiError(400, "bad_request",
                       "request body must be a JSON object")
    return data


def _require_confirm(data: dict, what: str) -> None:
    if data.get("confirm") is True:
        return
    raise ApiError(
        409, "confirm_required",
        f"this action changes a profile's session store and requires "
        f"\"confirm\": true in the request body to {what}",
        f"Confirmation required to {what}.",
    )


# --------------------------------------------------------------------------- #
# Pure row helpers
# --------------------------------------------------------------------------- #

def _row_to_session(row, context_window: int) -> dict:
    """Shape one raw `sessions` row into the API's session dict.

    Token totals mirror the columns SessionDB maintains per turn; ``total_tokens``
    is the sum across every accounted-for stream. The bloat verdict and the
    compaction headroom reuse the orchestrator's own decision machinery, so the
    sessions manager and the chat guard can never disagree about a session's
    health.
    """
    if hasattr(row, "keys"):           # sqlite3.Row (and dicts) -> plain dict
        row = dict(row)

    def _int(key):
        try:
            return int(row.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    input_tokens = _int("input_tokens")
    output_tokens = _int("output_tokens")
    cache_read_tokens = _int("cache_read_tokens")
    cache_write_tokens = _int("cache_write_tokens")
    reasoning_tokens = _int("reasoning_tokens")
    threshold_tokens = _orch.SESSION_COMPACTION_THRESHOLD_TOKENS

    bloated, reason = _orch._session_bloat_verdict(row)

    return {
        "id": row.get("id"),
        "title": row.get("title"),
        "source": row.get("source"),
        "model": row.get("model"),
        "message_count": _int("message_count"),
        "tool_call_count": _int("tool_call_count"),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": (input_tokens + output_tokens + cache_read_tokens
                         + cache_write_tokens + reasoning_tokens),
        "started_at": row.get("started_at"),
        "ended_at": row.get("ended_at"),
        "archived": bool(row.get("archived")),
        "pinned": bool(row.get("pinned")),
        "compression_failure_error":
            (row.get("compression_failure_error") or "").strip() or None,
        "compression_fallback_streak": _int("compression_fallback_streak"),
        "compression_ineffective_count": _int("compression_ineffective_count"),
        "context_window": context_window,
        "threshold_tokens": threshold_tokens,
        "compaction_headroom": context_window - threshold_tokens,
        "bloated": bloated,
        "reason": reason,
    }


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #

def _backing_list_sessions(profile: str, context_window: int) -> list | None:
    """List the profile's listable, non-archived sessions (newest first).

    Returns ``None`` when the profile is unresolvable / has no state.db (a
    200-with-honest-speak caller, never a crash). Opens the DB READ-ONLY — the
    safe choice for a status list over a busy profile's state.db.
    """
    db = _backing_open_profile_db(profile, read_only=True)
    if db is None:
        return None
    try:
        # The profile's own state.db — every row here is that profile's. Surface
        # the LISTABLE set (root/branch sessions; sub-agent runs hidden),
        # non-archived, newest first, mirroring the CLI's `/resume` mental model.
        rows = db._conn.execute(
            """
            SELECT * FROM sessions
            WHERE archived = 0
              AND (parent_session_id IS NULL)
            ORDER BY COALESCE(ended_at, started_at) DESC, started_at DESC, id DESC
            """
        ).fetchall()
    finally:
        db.close()
    return [_row_to_session(r, context_window) for r in rows]


def handle_sessions_list(server, ctx, query, body):
    """GET /v1/sessions — a profile's sessions with counts + headroom."""
    profile = (query.get("profile") or "").strip()
    if not profile:
        raise ApiError(400, "bad_request", "missing required query param 'profile'")
    try:
        _, context_window = _orch._session_guard_config(ctx)
    except Exception as exc:
        raise ApiError(500, "config_error", str(exc))
    sessions = _backing_list_sessions(profile, context_window)
    if sessions is None:
        return 200, {"profile": profile, "sessions": [],
                     "speak": f"Profile '{profile}' is not reachable."}
    bloated = [s for s in sessions if s["bloated"]]
    speak = (
        f"{len(sessions)} session{'s' if len(sessions) != 1 else ''} on "
        f"{profile}; {len(bloated)} at compaction risk."
    )
    return 200, {
        "profile": profile,
        "sessions": sessions,
        "count": len(sessions),
        "bloated_count": len(bloated),
        "speak": speak,
    }


# --------------------------------------------------------------------------- #
# Mutations (confirm-gated)
# --------------------------------------------------------------------------- #

def _resolve_session(db, session_id: str) -> dict:
    """Fetch one session row by id, or raise 404 — never guess."""
    try:
        row = db.get_session(session_id)
    except Exception:
        row = None
    if not row:
        raise ApiError(404, "not_found",
                       f"session '{session_id}' not found on this profile")
    return row


def handle_sessions_retire(server, ctx, query, body):
    """POST /v1/sessions/{id}/retire — non-destructive retirement (confirm-gated).

    Retitles the given session to ``<title>-retired-<ts>`` so it drops out of
    the live listable set while its full history stays on disk, intact —
    exactly the operator's manual recovery, applied to a chosen session by id.
    """
    session_id = query.get("session_id")
    data = _parse_body(body)
    _require_confirm(data, "retire this session")
    profile = (data.get("profile") or "").strip()
    if not profile:
        raise ApiError(400, "bad_request",
                       "missing required 'profile' in request body")
    db = _backing_open_profile_db(profile, read_only=False)
    if db is None:
        raise ApiError(404, "profile_unreachable",
                       f"profile '{profile}' has no session store",
                       "That profile has no sessions.")
    try:
        row = _resolve_session(db, session_id)
        title = row.get("title") or session_id
        import time as _time
        retired_title = f"{title}-retired-{_time.strftime('%Y%m%d-%H%M%S')}"
        db.set_session_title(session_id, retired_title)
        return 200, {
            "session_id": session_id,
            "previous_title": title,
            "retired_title": retired_title,
            "message": "session retired (history kept on disk)",
            "speak": f"Retired session '{title}' — its history is kept on disk "
                     f"as '{retired_title}'.",
        }
    finally:
        db.close()


def handle_sessions_compact(server, ctx, query, body):
    """POST /v1/sessions/{id}/compact — re-arm native compaction (confirm-gated).

    Keeps the session (continuity preserved): clears the compaction-failure
    latch (``compression_failure_error`` / ``compression_fallback_streak`` /
    ``compression_ineffective_count``) and re-ensures the profile's early
    threshold, so Hermes' own compressor retakes the floor on its next turn and
    shrinks the session for real. Only mutates the one named session.
    """
    session_id = query.get("session_id")
    data = _parse_body(body)
    _require_confirm(data, "re-arm compaction on this session")
    profile = (data.get("profile") or "").strip()
    if not profile:
        raise ApiError(400, "bad_request",
                       "missing required 'profile' in request body")
    _backing_ensure_threshold(profile)  # idempotent, fail-safe
    db = _backing_open_profile_db(profile, read_only=False)
    if db is None:
        raise ApiError(404, "profile_unreachable",
                       f"profile '{profile}' has no session store",
                       "That profile has no sessions.")
    try:
        row = _resolve_session(db, session_id)
        title = row.get("title") or session_id
        db._conn.execute(
            "UPDATE sessions SET "
            "compression_failure_error = NULL, "
            "compression_fallback_streak = 0, "
            "compression_ineffective_count = 0 "
            "WHERE id = ?",
            (session_id,),
        )
        db._conn.commit()
        return 200, {
            "session_id": session_id,
            "title": title,
            "message": "compaction re-armed (native compaction will fire on "
                       "the next turn)",
            "speak": f"Re-armed compaction on session '{title}' — the next "
                     f"turn will compact it for real.",
        }
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Route registration (import side-effect; loaded by api_server.py)
# --------------------------------------------------------------------------- #

ROUTES.append(("GET", re.compile(r"^/v1/sessions$"), handle_sessions_list))
ROUTES.append(
    ("POST", re.compile(r"^/v1/sessions/(?P<session_id>[^/]+)/retire$"),
     handle_sessions_retire)
)
ROUTES.append(
    ("POST", re.compile(r"^/v1/sessions/(?P<session_id>[^/]+)/compact$"),
     handle_sessions_compact)
)
