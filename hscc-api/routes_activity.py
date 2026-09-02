"""HSCC HTTP API — live agent activity feed (GET /v1/activity/feed).

The flight recorder across the fleet: who is running, which tool they just
called, on which card. Backed by the SAME real data the CLI verbs read —
nothing is re-implemented and nothing is fabricated:

  * which card each worker is on -> ``kill_switch.list_running_tasks()``
    (the running kanban tasks with board/id/title/assignee/pid);
  * the tool calls that profile just made -> its own Hermes ``state.db``
    ``messages`` table via the exact same ``_open_profile_session_db`` helper
    the sessions manager uses (routes_orchestrator).

The feed merges these two sources into one chronological timeline so an
operator can see "queued but GPU 0%" without touching a terminal. Each entry
carries enough to TAP-TO-TRACE: the ``profile`` (to open that profile's
sessions) and the ``card_id`` (to open that card) plus the ``session_id``.

Contract:

  * ``GET /v1/activity/feed?limit=N`` — the feed, newest first. ``limit`` caps
    the number of returned TOOL-CALL entries (default 50, max 200). Running
    rows are never truncated by ``limit`` — every running card is always in the
    timeline, so the on-screen ``speak``/``running_count`` never contradicts the
    visible list. Read-only (no
    ``confirm``). Always carries a top-level ``speak`` (design §B).
  * Two kinds of entry, both in one timeline:
      * ``kind = "running"``  — a worker is on a card (from the running-task
        list). Emitted once per running card even if that profile has no tool
        calls in the window, so "who is running what" is always visible.
      * ``kind = "tool_call"`` — a specific tool the profile just called
        (from its state.db messages), tied to a card when the profile maps to
        one.
  * Backing failures DEGRADE to a 200 with an honest ``speak`` (never a
    crash, never fabricated data) — matching the other ops reads.

Test seam: every backing call goes through a ``_backing_*`` module function
so tests can monkeypatch them without reading a live state.db or the live
kanban board.
"""

from __future__ import annotations

import json
import re

from api_server import ApiError, ROUTES

# --------------------------------------------------------------------------- #
# Backing-call seam (monkeypatch these in tests)
# --------------------------------------------------------------------------- #

def _backing_running_tasks():
    """Every running kanban card across all boards (board/id/title/assignee)."""
    from hscc_daemon import kill_switch
    return kill_switch.list_running_tasks()


def _backing_open_profile_db(profile: str, read_only: bool = True):
    """Open a profile's state.db read-only (or None when unresolvable)."""
    import routes_orchestrator as _orch
    return _orch._open_profile_session_db(profile, read_only=read_only)


# --------------------------------------------------------------------------- #
# Pure shapers
# --------------------------------------------------------------------------- #

def _tool_name_from_row(row) -> str | None:
    """Extract the tool name from a messages row.

    Prefers the ``tool_name`` column when set; otherwise parses the
    ``tool_calls`` JSON array for the first ``function.name`` (the shape
    observed in real Hermes state.db). Returns None when no tool name is
    present (a non-tool assistant row).
    """
    if not isinstance(row, dict):
        try:
            row = dict(row)
        except Exception:
            return None
    raw = row.get("tool_name")
    if raw:
        return str(raw)
    tc = row.get("tool_calls")
    if not tc:
        return None
    if isinstance(tc, str):
        try:
            tc = json.loads(tc)
        except ValueError:
            return None
    if isinstance(tc, list):
        for call in tc:
            if not isinstance(call, dict):
                continue
            # Common shape: {"function": {"name": "..."}, ...}.
            fn = call.get("function")
            if isinstance(fn, dict) and fn.get("name"):
                return str(fn["name"])
            # Fallback: {"name": "..."} directly.
            if call.get("name"):
                return str(call["name"])
    return None


def _iso_timestamp(row) -> str | None:
    """Format a messages row's unix ``timestamp`` as UTC ISO, or None."""
    try:
        raw = row.get("timestamp")
    except Exception:
        return None
    if raw is None:
        return None
    import datetime
    try:
        ts = float(raw)
    except (TypeError, ValueError):
        return None
    try:
        return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat()
    except (OSError, ValueError, OverflowError):
        return None


def _profile_to_running(running_tasks: list) -> dict:
    """Map profile name -> its first running card dict.

    A profile with several running cards keeps the newest; multiple cards on
    one worker is the normal case for an orchestrator, and the feed shows the
    card context without duplicating every tool call per card.
    """
    mapping: dict = {}
    for t in running_tasks:
        assignee = (t.get("assignee") or "").strip()
        if not assignee:
            continue
        if assignee not in mapping:
            mapping[assignee] = t
    return mapping


def _recent_tool_calls(db, profile: str, limit: int) -> list:
    """Pull the profile's most recent tool-call messages from its state.db.

    Queries its ``messages`` table (via the already-open read-only DB) for
    the most recent assistant rows that carry a tool call, newest first.
    Returns a list of raw dict rows. The caller owns the connection's
    lifecycle — this function never opens or closes it.
    """
    rows = db._conn.execute(
        """
        SELECT session_id, role, tool_name, tool_calls, timestamp
        FROM messages
        WHERE role = 'assistant'
          AND (tool_name IS NOT NULL OR tool_calls IS NOT NULL)
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def _build_feed(running_tasks: list, limit: int) -> dict:
    """Assemble the activity timeline from running cards + per-profile tool calls.

    Returns {entries: [...], count, running_count, profiles} — the payload
    minus ``speak``. Pure enough to unit test directly.
    """
    by_profile = _profile_to_running(running_tasks)
    entries: list = []

    # 1) A "running" row per running card — even with no tool call in the
    #    window, the operator must see who is on what.
    for t in running_tasks:
        entries.append({
            "kind": "running",
            "profile": t.get("assignee"),
            "board": t.get("board"),
            "card_id": t.get("id"),
            "card_title": t.get("title"),
            "pid": t.get("pid"),
            "host_local": t.get("host_local"),
            "started_at": t.get("started_at"),
            "at": t.get("started_at"),
            "tool": None,
            "session_id": None,
        })

    # 2) The most recent tool calls per running profile, tied to its card.
    for profile, card in by_profile.items():
        db = _backing_open_profile_db(profile, read_only=True)
        if db is None:
            continue
        try:
            rows = _recent_tool_calls(db, profile, limit)
        except Exception:
            rows = []
        finally:
            try:
                db.close()
            except Exception:
                pass
        for r in rows:
            tool = _tool_name_from_row(r)
            if not tool:
                continue
            entries.append({
                "kind": "tool_call",
                "profile": profile,
                "board": card.get("board"),
                "card_id": card.get("id"),
                "card_title": card.get("title"),
                "pid": card.get("pid"),
                "host_local": card.get("host_local"),
                "started_at": card.get("started_at"),
                "at": _iso_timestamp(r),
                "tool": tool.split(".")[0] if "." in tool else tool,
                "session_id": r.get("session_id"),
            })

    # Newest first across BOTH kinds. Entries carry "at" as an ISO timestamp
    # (tool_call = the tool call's time; running = the card's started_at).
    # Sort by (has_timestamp, timestamp-string) descending so a missing
    # timestamp sinks to the bottom rather than bubbling to the top.
    entries.sort(key=lambda e: _norm_sort(e.get("at")), reverse=True)

    # `limit` caps TOOL-CALL entries only. A "running" row is one per running
    # card and cheap; it must ALWAYS survive the cap. Otherwise a saturated
    # timeline (many tool calls) truncates the running rows, and the on-screen
    # `speak` ("N running tasks") — which counts ALL running tasks, see
    # `running_count` below — contradicts the visible list (fewer Running
    # badges). Keeping every running row preserves the route's stated contract:
    # "who is running what is always visible".
    tool_rows = [e for e in entries if e.get("kind") != "running"][:limit]
    running_rows = [e for e in entries if e.get("kind") == "running"]
    entries = running_rows + tool_rows
    entries.sort(key=lambda e: _norm_sort(e.get("at")), reverse=True)

    profiles = sorted(by_profile.keys())
    return {
        "entries": entries,
        "count": len(entries),
        "running_count": len(running_tasks),
        "profiles": profiles,
    }


def _norm_sort(at):
    """A stable sort key for a timestamp string, or a sentinel that sinks it."""
    if not at:
        return (0, "")
    return (1, str(at))


# --------------------------------------------------------------------------- #
# speak helpers (design §B) — pure
# --------------------------------------------------------------------------- #

def _speak_feed(payload: dict) -> str:
    """§B: \"{n} events across {m} running worker(s).\" / honest empty/degrade."""
    running = payload.get("running_count") or 0
    n = payload.get("count") or 0
    if running == 0:
        return "No agents currently running."
    return (f"{n} activity event{'s' if n != 1 else ''} across "
            f"{running} running task{'s' if running != 1 else ''}.")


# --------------------------------------------------------------------------- #
# Handler
# --------------------------------------------------------------------------- #

def handle_activity_feed(server, ctx, query, body):
    """GET /v1/activity/feed — the live agent activity feed (flight recorder)."""
    raw_limit = query.get("limit")
    limit = 50
    if raw_limit is not None:
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            raise ApiError(
                400, "bad_request",
                "limit must be a non-negative integer",
                "Limit must be a non-negative integer.",
            )
        if limit < 0:
            raise ApiError(
                400, "bad_request",
                "limit must be a non-negative integer",
                "Limit must be a non-negative integer.",
            )
        if limit > 200:
            limit = 200
    limit = max(limit, 1)

    try:
        running = _backing_running_tasks()
        tasks = running.get("tasks", []) if isinstance(running, dict) else []
    except Exception:
        tasks = []
    payload = _build_feed(tasks, limit)
    payload["speak"] = _speak_feed(payload)
    return 200, payload


# --------------------------------------------------------------------------- #
# Route registration (import side-effect; loaded by api_server.py)
# --------------------------------------------------------------------------- #

ROUTES.append(("GET", re.compile(r"^/v1/activity/feed$"), handle_activity_feed))
