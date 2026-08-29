"""HSCC API — session history endpoint (t_47f51a71 bridge contract).

Read-only router that serves a project's chat event log to the iOS client.
This is the FIRST committed piece of the WebSocket-bridge card: the wire
contract (frame shapes, seq cursor semantics) and the history read half, so
the downstream typed-event view (t_1ff4dcbd) and history pager (t_2776ea3c)
can build against a pinned, committed contract. The live WebSocket relay half
(``ws  <->  hermes serve``) is the follow-on increment within this same card —
it appends to the SAME per-project store this endpoint reads, giving one
contiguous seq space across history and live.

Endpoint:

    GET /v1/projects/{name}/session/events?before=<seq>&limit=<n>

    * ``before`` (optional) — exclusive upper seq bound; returns only events
      with ``seq < before``. Omitted = newest page (the tail).
    * ``limit`` (optional, default 200) — max frames returned.
    * Response (200)::

        {
          "project": "<name>",
          "events": [ <frame>, ... ],     // seq ASCENDING
          "next_before": <seq|null>,      // cursor for the next OLDER page
          "oldest_seq": <seq>,            // first retained frame
          "next_seq": <seq>,              // current high-water mark
          "speak": "10 events in <name>."
        }

    A client that has seen up to ``seq s`` calls ``before=s`` and then
    subscribes to the WebSocket from ``s+1`` — no gap, no duplicate
    (t_218cb9ec).

Conventions (shared, design §A): handlers are ``(server, ctx, query, body) ->
(status, payload_dict)``; unknown project -> 404 ``not_found``; ``speak`` is
always present on a read response. The backing store is pure in-memory and
import-safe with no I/O, so this endpoint is testable in isolation.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from api_server import ApiError, ROUTES          # noqa: E402
from session_event import get_store              # noqa: E402

# Make the relocated flightdeck importable, exactly like routes_project does
# (insert hscc-project/ on sys.path once). Needed when this module's tests or
# the handler run in isolation without routes_project already imported.
_PROJECT_DIR = Path(__file__).resolve().parent.parent / "hscc-project"
if _PROJECT_DIR.is_dir() and str(_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_PROJECT_DIR))

from flightdeck.core import registry as _registry  # noqa: E402
from routes_project import _registry_path           # noqa: E402


def _speak_history(data: dict, project: str) -> str:
    events = data.get("events") or []
    n = len(events)
    noun = "event" if n == 1 else "events"
    if n == 0:
        return f"No session events for {project}."
    return f"{n} {noun} in {project}."


def handle_session_events(server, ctx, query, body):
    """GET /v1/projects/{name}/session/events — page the project's chat log."""
    name = query.get("name")
    if name is None or not str(name).strip():
        raise ApiError(400, "bad_request", "missing project name")

    # Canonicalize the project so unknown names 404 like every other
    # /v1/projects/{name} endpoint (the flightdeck registry is the source of
    # truth for which names exist).
    try:
        _registry.get_project(name, path=_registry_path(ctx))
    except _registry.ProjectNotFoundError:
        raise ApiError(
            404, "not_found", f"no project named {name!r}",
            f"Project {name} was not found.",
        )

    # Query params.
    before = query.get("before")
    limit = query.get("limit")

    def _as_int(v, what: str) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            raise ApiError(
                400, "bad_request", f"{what} must be a positive integer")

    if before is not None:
        before = _as_int(before, "before")
        if before < 1:
            raise ApiError(400, "bad_request", "before must be >= 1")
    if limit is not None:
        limit = _as_int(limit, "limit")
        if limit < 0:
            raise ApiError(400, "bad_request", "limit must be >= 0")
    else:
        limit = 200
    if limit > 1000:
        limit = 1000

    store = get_store(name)
    data = store.history(before=before, limit=limit)
    payload = {
        "project": name,
        "events": data["events"],
        "next_before": data["next_before"],
        "oldest_seq": data["oldest_seq"],
        "next_seq": data["next_seq"],
    }
    payload["speak"] = _speak_history(payload, name)
    return 200, payload


# Register against the api_server route table (the plugin's route modules each
# append their handlers; api_server imports them at build time — verify that
# import wiring below).
ROUTES.append((
    "GET",
    re.compile(r"^/v1/projects/(?P<name>[^/]+)/session/events$"),
    handle_session_events,
))
