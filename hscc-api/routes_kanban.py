"""HSCC HTTP API — Kanban board-hygiene endpoints (blocked / recover / stale).

Wraps the SAME ``hscc_daemon`` board-hygiene modules the ``hscc kanban`` CLI
verb uses (``kanban_blocked.py`` for blocked/recover, ``autodown.py`` for
stale) — never re-implements board enumeration. Follows the existing API
contract:

  * handlers are ``(server, ctx, query, body) -> (status, dict)``;
  * reads carry a top-level ``speak`` (design §B);
  * the RECOVER mutation requires ``confirm: true`` in the body (409
    ``confirm_required`` otherwise). Recovery is never automatic and never
    bulk — exactly ONE task id, and only a human decides a blocked card is
    safe to re-run.

Backing (libraries, never CLI text-parsing):
  * ``GET  /v1/kanban/blocked``           -> ``kanban_blocked.list_blocked_tasks()``
  * ``POST /v1/kanban/blocked/{id}/recover`` -> ``kanban_blocked.recover_blocked_task(id, reason)``
  * ``GET  /v1/kanban/stale?older_than=N`` -> ``autodown.list_stale_tasks(older_than=N)``

Test seam: every backing call goes through a ``_backing_*`` module function so
tests can monkeypatch them without scanning the live kanban DB or mutating a
real card.
"""

from __future__ import annotations

import re

from api_server import ApiError, ROUTES


# --------------------------------------------------------------------------- #
# Backing-call seam (monkeypatch these in tests)
# --------------------------------------------------------------------------- #

def _backing_list_blocked():
    from hscc_daemon import kanban_blocked
    return kanban_blocked.list_blocked_tasks()


def _backing_recover_blocked(task_id, reason=None):
    from hscc_daemon import kanban_blocked
    return kanban_blocked.recover_blocked_task(task_id, reason=reason)


def _backing_list_stale(older_than=None, board=None):
    from hscc_daemon import autodown
    return autodown.list_stale_tasks(older_than=older_than, board=board)


# --------------------------------------------------------------------------- #
# Body helpers (confirm gate mirrors routes_actions)
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
        f"this action mutates the shared kanban board and requires "
        f"\"confirm\": true in the request body to {what}",
        f"Confirmation required to {what}.",
    )


# --------------------------------------------------------------------------- #
# speak helpers (design §B) — pure
# --------------------------------------------------------------------------- #

def _speak_blocked(data: dict) -> str:
    """§B: "{n} card(s) blocked on {b} board(s)." / "no blocked cards."."""
    tasks = data.get("tasks") or []
    if not tasks:
        return "No blocked cards on any board."
    n = len(tasks)
    return (f"{n} card{'s' if n != 1 else ''} blocked across "
            f"{data.get('boards', 0)} board{'s' if data.get('boards', 0) != 1 else ''}.")


def _speak_stale(data: dict) -> str:
    """§B: "{n} stale card(s)." / "no stale cards."."""
    tasks = data.get("tasks") or []
    if not tasks:
        return "No stale cards."
    return f"{len(tasks)} stale card{'s' if len(tasks) != 1 else ''}."


def _speak_recover(payload: dict) -> str:
    """§B: "Recovered card {id} to ready."."""
    return f"Recovered card {payload.get('id')} to ready."


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #

def handle_kanban_blocked(server, ctx, query, body):
    """GET /v1/kanban/blocked — blocked cards across ALL boards + why."""
    try:
        data = _backing_list_blocked()
    except Exception:
        return 200, {"speak": "Blocked-card list unavailable."}
    if not isinstance(data, dict):
        return 200, {"speak": "Blocked-card list unavailable."}
    payload = {
        "boards": data.get("boards", 0),
        "tasks": data.get("tasks", []),
        "errors": data.get("errors", []),
        "count": len(data.get("tasks", [])),
    }
    payload["speak"] = _speak_blocked(payload)
    return 200, payload


def handle_kanban_recover(server, ctx, query, body):
    """POST /v1/kanban/blocked/{card_id}/recover — recover ONE blocked card (confirm-gated)."""
    data = _parse_body(body)
    _require_confirm(data, "recover this blocked card")
    card_id = query.get("card_id")
    if card_id is None or not str(card_id).strip():
        raise ApiError(400, "bad_request", "missing card_id")
    reason = data.get("reason")
    try:
        label, ok = _backing_recover_blocked(card_id, reason=reason)
    except Exception as exc:
        raise ApiError(502, "recover_failed", str(exc),
                       "Card could not be recovered.")
    if not ok:
        raise ApiError(
            404, "not_found",
            f"card {card_id!r} is not a blocked task on any board",
            f"Card {card_id} could not be recovered — it is not blocked.",
        )
    payload = {
        "id": card_id,
        "board": label,
        "reason": reason,
        "message": f"recovered {card_id} (board '{label}') to ready",
    }
    payload["speak"] = _speak_recover(payload)
    return 200, payload


def handle_kanban_stale(server, ctx, query, body):
    """GET /v1/kanban/stale — non-terminal cards across all boards.

    ``?older_than=`` (days) defaults to ``autodown.DEFAULT_STALE_DAYS``; pass
    0 for ALL non-terminal cards.
    """
    raw = query.get("older_than")
    if raw is None:
        from hscc_daemon import autodown
        older_than = autodown.DEFAULT_STALE_DAYS
    else:
        try:
            older_than = int(raw)
        except (ValueError, TypeError):
            raise ApiError(
                400, "bad_request",
                "older_than must be a non-negative integer",
                "Older-than must be a non-negative integer.",
            )
        if older_than < 0:
            raise ApiError(
                400, "bad_request",
                "older_than must be a non-negative integer",
                "Older-than must be a non-negative integer.",
            )
    try:
        data = _backing_list_stale(older_than=older_than)
    except Exception:
        return 200, {"speak": "Stale-card list unavailable."}
    if not isinstance(data, dict):
        return 200, {"speak": "Stale-card list unavailable."}
    payload = {
        "boards": data.get("boards", 0),
        "tasks": data.get("tasks", []),
        "errors": data.get("errors", []),
        "older_than": older_than,
        "count": len(data.get("tasks", [])),
    }
    payload["speak"] = _speak_stale(payload)
    return 200, payload


# --------------------------------------------------------------------------- #
# Route registration (import side-effect; loaded by api_server.py)
# --------------------------------------------------------------------------- #

ROUTES.append(("GET", re.compile(r"^/v1/kanban/blocked$"),
               handle_kanban_blocked))
ROUTES.append(
    ("POST", re.compile(r"^/v1/kanban/blocked/(?P<card_id>[^/]+)/recover$"),
     handle_kanban_recover)
)
ROUTES.append(("GET", re.compile(r"^/v1/kanban/stale$"),
               handle_kanban_stale))
