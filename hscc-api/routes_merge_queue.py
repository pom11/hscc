"""GET /v1/merge-queue — the local merge queue's rows (read-only).

Backing is ``hscc_daemon.merge_queue.view()`` — the SAME read
``hscc kanban monitor`` renders as its per-project "merge queue" tables and
``hscc merge-queue status`` reads its ledger through. No handler-local copy of
the queue projection: FIFO positions, live stages, ages and last results come
from the one function, so the route and the CLI cannot disagree.

The queue is local (``~/.hscc/merge-queue-state.json``) and read-only from
here; only the daemon tick runs git/gh in its own worktree. ``?board=`` narrows
to the project whose registry board matches, mirroring ``hscc kanban monitor
--board``.

Route map + field table: ``docs/WIDGET_ROUTES.md``.
"""

from __future__ import annotations

import re

from api_server import ROUTES


def _backing_view(board=None):
    """The merge queue's rows — one call shared with the kanban monitor."""
    from hscc_daemon import merge_queue
    return merge_queue.view(board=board)


def _speak(data: dict) -> str:
    rows = data.get("rows") or []
    queued = data.get("queued")
    if queued is None:
        queued = sum(1 for row in rows if row.get("state") == "queued")
    if not rows:
        return "Merge queue is empty."
    return (f"{len(rows)} merge-queue row{'s' if len(rows) != 1 else ''}, "
            f"{queued} queued.")


def handle_merge_queue(server, ctx, query, body):
    """GET /v1/merge-queue — read-only queue rows, token-gated like every route."""
    board = query.get("board")
    try:
        data = _backing_view(board=board)
    except Exception:
        return 200, {"ok": False, "projects": {}, "rows": [], "count": 0,
                     "queued": 0, "speak": "Merge queue unavailable."}
    payload = {
        "ok": bool(data.get("ok", True)),
        "generated_at": data.get("generated_at"),
        "projects": data.get("projects") or {},
        "rows": data.get("rows") or [],
        "count": data.get("count", len(data.get("rows") or [])),
        "queued": data.get("queued", 0),
    }
    payload["speak"] = _speak(payload)
    return 200, payload


ROUTES.append(("GET", re.compile(r"^/v1/merge-queue$"),
               handle_merge_queue))
