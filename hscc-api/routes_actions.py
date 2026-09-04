"""HSCC HTTP API — Phase A4: mutating (confirm-gated) endpoints.

Registers the mutating POST endpoints on ``api_server.ROUTES`` (see
docs/DESIGN-api.md §A "Actions (mutating, confirm-gated)"). Every endpoint
here REQUIRES ``"confirm": true`` in the JSON body — anything else returns 409
``confirm_required``, mirroring the CLI's ``--apply``/``--confirm`` gate. A GET
must never reach these (they are registered as POST only).

Backing (libraries, never CLI text-parsing — the same rule as A2/A3):
  * ``POST /v1/cards``              -> ``flightdeck.core.kanban.create_task``
  * ``POST /v1/review/{id}/merge``  -> ``flightdeck.commands.review``
      ``_do_apply`` then ``_real_close_card`` (close ONLY once the merge landed)
  * ``POST /v1/template/apply``     -> ``cluster_template_cli.cmd_cluster_template``
      exactly as ``hscc_daemon/hscc.py:_handle_template`` loads it
  * ``POST /v1/cluster/stop``       -> ``hscc-cluster.hscc.cmd_stop``
  * ``POST /v1/cards/{id}/comment`` -> ``flightdeck.core.kanban.add_card_comment``
  * ``POST /v1/cards/{id}/block``   -> ``flightdeck.core.kanban.block_card``
  * ``POST /v1/cards/{id}/close``   -> ``flightdeck.core.kanban.close_card``
  * ``PATCH /v1/cards/{id}``        -> ``flightdeck.core.kanban.edit_card`` (assignee only)
      — title/body are NOT backed by a kanban_db mutation, so only assignee is editable

Test seam: every backing call goes through a ``_backing_*`` module function so
tests can monkeypatch them without ever creating a card, merging a branch,
applying a template, or stopping a container. The unit tests assert the backing
call was NOT made when confirm is missing, and that a FAILED merge does not
close the card.

Mutating responses carry a human ``"message"`` plus structured fields (per the
design); they do NOT carry ``speak``.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from api_server import ApiError, ROUTES

# Make the relocated flightdeck available exactly like routes_project.py does
# (and like ``hscc_daemon/hscc.py:_handle_project``). Insert ``hscc-project/``
# on sys.path once, then import the modules directly.
_PROJECT_DIR = None
for _candidate in (
    Path(__file__).resolve().parent.parent / "hscc-project",  # live checkout
):
    if _candidate.is_dir():
        _PROJECT_DIR = _candidate
        break
if _PROJECT_DIR is not None and str(_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_PROJECT_DIR))

from flightdeck.core import kanban as _kanban            # noqa: E402
from flightdeck.commands import review as _review_cmd    # noqa: E402

from routes_cluster import _load_cluster_engine          # noqa: E402
from routes_project import _registry_path                # noqa: E402

# The base branch merges land on (matches flightdeck review / routes_project).
_DEFAULT_BASE = "main"


# --------------------------------------------------------------------------- #
# Body parsing helpers
# --------------------------------------------------------------------------- #

def _parse_body(body: bytes) -> dict:
    """Decode the request body as JSON; 400 on empty or malformed.

    A malformed or non-object body is a contract error (bad_request), never a
    crash. Returns {} for an empty body so handlers can uniformly detect the
    missing ``confirm`` field -> 409.
    """
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
    """Return None (pass) if ``confirm`` is exactly true, else raise 409.

    ``what`` is a short phrase naming the action, used to build the human
    ``message`` on the 409 (e.g. ``"dispatch a card"``). The ``confirm`` gate
    is the whole point of the Actions contract — it mirrors the CLI's
    ``--apply``/``--confirm`` guard so the API can never mutate by accident.
    """
    if data.get("confirm") is True:
        return
    raise ApiError(
        409, "confirm_required",
        f"this action is destructive and requires \"confirm\": true in the "
        f"request body to {what}",
        f"Confirmation required to {what}.",
    )


# --------------------------------------------------------------------------- #
# Backing-call seam (monkeypatch these in tests)
# --------------------------------------------------------------------------- #

def _backing_create_task(board, title, assignee=None, body=None, _kdb=None):
    """Dispatch a card via flightdeck ``kanban.create_task``."""
    return _kanban.create_task(board, title, assignee=assignee, body=body, _kdb=_kdb)


def _backing_add_comment(card_id, body, author=None, _kdb=None):
    """Add a comment via flightdeck ``kanban.add_card_comment``; returns comment id."""
    return _kanban.add_card_comment(card_id, body, author=author, _kdb=_kdb)


def _backing_block_card(card_id, reason=None, kind=None, _kdb=None):
    """Block a card via flightdeck ``kanban.block_card``; returns bool."""
    return _kanban.block_card(card_id, reason, kind=kind, _kdb=_kdb)


def _backing_complete_card(card_id, result=None, _kdb=None):
    """Complete/close a card via flightdeck ``kanban.close_card``; returns bool.

    Named ``complete`` (not ``close``) to avoid colliding with the pre-existing
    ``_backing_close_card(card_id, board)`` seam used by the merge handler
    (which archives via ``_real_close_card``). This one completes via
    ``kanban_db.complete_task``.
    """
    return _kanban.close_card(card_id, result=result, _kdb=_kdb)


def _backing_edit_card(card_id, assignee=None, _kdb=None):
    """Edit a card (assignee) via flightdeck ``kanban.edit_card``; returns bool."""
    return _kanban.edit_card(card_id, assignee=assignee, _kdb=_kdb)


def _backing_resolve_card(card_id, ctx):
    """Resolve card_id -> (card, project, branch); ReviewError -> unresolvable.

    Mirrors routes_project's review-detail path: read cards + the registry
    (config-driven path), then ``flightdeck.commands.review._resolve``. Let the
    handler map ``ReviewError`` to 404.
    """
    cards = _kanban.list_cards()
    projects = _load_registry(ctx)
    return _review_cmd._resolve(cards, projects, card_id)


def _load_registry(ctx):
    """Load the registry through routes_project's config-driven path."""
    registry_path = _registry_path(ctx)
    # Import lazily: registry is a flightdeck.core module used only for merges.
    from flightdeck.core import registry as _registry
    return _registry.load_registry(registry_path)


def _backing_is_merged(repo, branch, base=_DEFAULT_BASE):
    """Whether ``branch`` is already an ancestor of ``base`` (git_state)."""
    return _review_cmd.git_state.is_merged(repo, branch, base)


def _backing_do_apply(repo, branch, base=_DEFAULT_BASE):
    """Merge ``branch`` into ``base`` and push ``base``; returns outcome string."""
    return _review_cmd._do_apply(repo, branch, base)


def _backing_close_card(card_id, board):
    """Archive/close the card; returns True only when the archive landed."""
    return _review_cmd._real_close_card(card_id, board)


def _backing_template_apply(name, force_recreate=False):
    """Apply a cluster template, exactly as ``_handle_template`` loads it.

    Puts ``hscc-cluster/`` on sys.path (the engine's submodules import each
    other by bare name), imports ``cluster_template_cli.cmd_cluster_template``,
    and calls ``cmd_cluster_template(["apply", name, "--confirm", ...])``. The
    ``--confirm`` flag is the CLI's own confirm gate; the API ALSO enforces the
    HTTP-level ``confirm: true`` (in the handler) before reaching here.
    """
    cluster_dir = Path(__file__).resolve().parent.parent / "hscc-cluster"
    if str(cluster_dir) not in sys.path:
        sys.path.insert(0, str(cluster_dir))
    from cluster_template_cli import cmd_cluster_template
    args = ["apply", name, "--confirm"]
    if force_recreate:
        args.append("--force-recreate")
    return cmd_cluster_template(args)


def _backing_stop(container_id):
    """Stop a running workload via the shared cluster engine (cmd_stop)."""
    eng = _load_cluster_engine()
    if eng is None:
        return {"success": False, "error": "hscc-cluster plugin not found"}
    return eng.cmd_stop(container_id)


# --------------------------------------------------------------------------- #
# Shared action preamble
# --------------------------------------------------------------------------- #

def _action_fields(body: bytes, what: str, *required):
    """Parse + validate the common action body.

    Returns the parsed dict after enforcing ``confirm: true`` (409) and that
    every ``*required`` field is present and non-empty (400).
    """
    data = _parse_body(body)
    _require_confirm(data, what)
    for field in required:
        value = data.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ApiError(
                400, "bad_request",
                f"missing required field {field!r}",
                f"Field {field} is required.",
            )
    return data


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #

def handle_create_card(server, ctx, query, body):
    """POST /v1/cards — dispatch a card (confirm-gated)."""
    data = _action_fields(body, "dispatch a card", "board", "title")
    card_id = _backing_create_task(
        data["board"], data["title"],
        assignee=data.get("assignee"), body=data.get("body"),
    )
    return 200, {
        "id": str(card_id),
        "message": f"dispatched card {card_id}",
    }


def handle_merge_card(server, ctx, query, body):
    """POST /v1/review/{card_id}/merge — merge branch + close card (confirm-gated).

    Mirrors ``flightdeck.commands.review.cmd_review``'s ``--apply`` ordering:
    resolve the card, merge its branch into ``main``, and ONLY close the card
    when the merge ACTUALLY landed. On a failed/partial merge the card stays
    open and we return a non-2xx surfacing the failure — never report success
    for a merge that didn't land.
    """
    data = _parse_body(body)
    _require_confirm(data, "merge this card")
    card_id = query.get("card_id")
    if card_id is None or not str(card_id).strip():
        raise ApiError(400, "bad_request", "missing card_id")

    # Resolve the card -> its repo/branch (404 when unresolvable), mirroring
    # the review-detail read path.
    try:
        card, project, branch = _backing_resolve_card(card_id, ctx)
    except _review_cmd.ReviewError:
        raise ApiError(
            404, "not_found",
            f"card {card_id!r} does not resolve to a reviewable branch",
            f"Card {card_id} could not be resolved.",
        )

    repo = project.repo
    board = card.get("board")

    # REFUSE if already landed — mirror cmd_review (review.py:439-456): a merged
    # branch has nothing to merge, so we do NOT re-merge and do NOT close.
    if _backing_is_merged(repo, branch):
        raise ApiError(
            409, "already_landed",
            f"card {card_id} is already merged into {_DEFAULT_BASE}",
            f"Card {card_id} is already merged.",
        )

    outcome = _backing_do_apply(repo, branch, _DEFAULT_BASE)
    # Mirror cmd_review (review.py:478): on a failed or partial merge there is
    # nothing to close. Surface the failure as a non-2xx and keep the card open.
    if outcome.startswith("merge failed") or outcome.startswith("merge done"):
        raise ApiError(
            502, "merge_failed", outcome,
            f"Merge failed: {outcome}.",
        )

    # Merge landed. Close the card (archive it). A False return means the
    # archive genuinely failed (e.g. card already archived) — mirror cmd_review
    # by still reporting the landed merge as a 2xx success but flagging the
    # close as a warning rather than claiming card_closed.
    close_ok = bool(_backing_close_card(card_id, board))
    if not close_ok:
        return 200, {
            "message": (
                f"merged card {card_id} into {_DEFAULT_BASE} but the card "
                f"could not be archived (may already be closed)"
            ),
            "merged": True,
            "card_closed": False,
            "warning": "card could not be archived",
        }
    return 200, {
        "message": f"merged card {card_id} into {_DEFAULT_BASE} and closed it",
        "merged": True,
        "card_closed": True,
    }


def handle_template_apply(server, ctx, query, body):
    """POST /v1/template/apply — apply a cluster template (confirm-gated)."""
    data = _action_fields(body, "apply a template", "name")
    name = data["name"]
    force_recreate = bool(data.get("force_recreate"))
    result = _backing_template_apply(name, force_recreate=force_recreate)

    # Mirror the v1.8.4 exit-code lesson from _handle_template: a BLOCKED or
    # PARTIALLY-applied apply (result success False) must NOT be reported as a
    # 2xx success — surface it as an error.
    if not isinstance(result, dict) or result.get("success") is not True:
        reason = (
            str(result.get("error"))
            if isinstance(result, dict) and result.get("error")
            else f"template {name!r} could not be fully applied"
        )
        raise ApiError(502, "apply_failed", reason, f"Template {name} did not apply cleanly.")
    payload = dict(result)
    payload["message"] = f"applied template {name}"
    return 200, payload


def handle_cluster_stop(server, ctx, query, body):
    """POST /v1/cluster/stop — stop a running workload (confirm-gated)."""
    data = _action_fields(body, "stop this workload", "container_id")
    container_id = data["container_id"]
    result = _backing_stop(container_id)
    if not isinstance(result, dict) or result.get("success") is not True:
        reason = (
            str(result.get("error"))
            if isinstance(result, dict) and result.get("error")
            else f"could not stop container {container_id!r}"
        )
        raise ApiError(502, "stop_failed", reason, f"Container {container_id} could not be stopped.")
    return 200, {
        "message": f"stopped container {container_id}",
        "container_id": container_id,
        **result,
    }


def _resolve_card_or_404(card_id):
    """Resolve a card via flightdeck ``find_card``; 404 when not found.

    ``find_card`` returns a flightdeck card dict (with ``board`` / optionally
    ``_board_path``) or None. Backing functions re-open the owning board's DB
    via that path, so this handler only needs confirmation the card EXISTS.
    """
    card = _kanban.find_card(card_id)
    if card is None:
        raise ApiError(
            404, "not_found",
            f"card {card_id!r} not found",
            f"Card {card_id} could not be found.",
        )
    return card


def handle_card_comment(server, ctx, query, body):
    """POST /v1/cards/{card_id}/comment — comment on a card (confirm-gated).

    Requires ``body`` (the comment text) AND ``author`` (the DB's
    ``kanban_db.add_comment`` hard-requires a non-empty author). Returns the
    new comment id.
    """
    data = _parse_body(body)
    _require_confirm(data, "comment on this card")
    card_id = query.get("card_id")
    if card_id is None or not str(card_id).strip():
        raise ApiError(400, "bad_request", "missing card_id")
    comment_body = data.get("body")
    if comment_body is None or (isinstance(comment_body, str) and not comment_body.strip()):
        raise ApiError(400, "bad_request", "missing required field 'body'", "Field body is required.")
    comment_author = data.get("author")
    if comment_author is None or (isinstance(comment_author, str) and not comment_author.strip()):
        raise ApiError(400, "bad_request", "missing required field 'author'", "Field author is required.")
    _resolve_card_or_404(card_id)
    try:
        comment_id = _backing_add_comment(card_id, comment_body, author=comment_author)
    except Exception as exc:
        raise ApiError(
            502, "comment_failed", str(exc), f"Comment could not be added to card {card_id}."
        )
    return 200, {
        "id": str(card_id),
        "comment_id": int(comment_id),
        "message": f"added comment to card {card_id}",
    }


def handle_card_block(server, ctx, query, body):
    """POST /v1/cards/{card_id}/block — block a card (confirm-gated).

    Requires ``reason``. ``kind`` is optional and passed through to
    ``kanban_db.block_task``.
    """
    data = _parse_body(body)
    _require_confirm(data, "block this card")
    card_id = query.get("card_id")
    if card_id is None or not str(card_id).strip():
        raise ApiError(400, "bad_request", "missing card_id")
    reason = data.get("reason")
    if reason is None or (isinstance(reason, str) and not reason.strip()):
        raise ApiError(400, "bad_request", "missing required field 'reason'", "Field reason is required.")
    _resolve_card_or_404(card_id)
    try:
        ok = bool(_backing_block_card(card_id, reason=reason, kind=data.get("kind")))
    except Exception as exc:
        raise ApiError(
            502, "block_failed", str(exc), f"Card {card_id} could not be blocked."
        )
    if not ok:
        return 200, {
            "id": str(card_id),
            "blocked": False,
            "message": f"card {card_id} could not be blocked (may already be blocked)",
        }
    return 200, {
        "id": str(card_id),
        "blocked": True,
        "message": f"blocked card {card_id}",
    }


def handle_card_close(server, ctx, query, body):
    """POST /v1/cards/{card_id}/close — complete/close a card (confirm-gated).

    ``result`` is optional and passed through to ``kanban_db.complete_task``.
    """
    data = _parse_body(body)
    _require_confirm(data, "close this card")
    card_id = query.get("card_id")
    if card_id is None or not str(card_id).strip():
        raise ApiError(400, "bad_request", "missing card_id")
    _resolve_card_or_404(card_id)
    try:
        ok = bool(_backing_complete_card(card_id, result=data.get("result")))
    except Exception as exc:
        raise ApiError(
            502, "close_failed", str(exc), f"Card {card_id} could not be closed."
        )
    if not ok:
        return 200, {
            "id": str(card_id),
            "closed": False,
            "message": f"card {card_id} could not be closed (may already be closed)",
        }
    return 200, {
        "id": str(card_id),
        "closed": True,
        "message": f"closed card {card_id}",
    }


def handle_card_edit(server, ctx, query, body):
    """PATCH /v1/cards/{card_id} — edit a card (confirm-gated, assignee only).

    NOTE: only ``assignee`` is editable. kanban_db exposes no backing mutation
    for a card's ``title`` or ``body``, so those fields are not accepted here —
    editing them via this route is not backed and is out of scope (see
    ``flightdeck.core.kanban.edit_card``). Pass ``assignee`` to re-assign.
    """
    data = _parse_body(body)
    _require_confirm(data, "edit this card")
    card_id = query.get("card_id")
    if card_id is None or not str(card_id).strip():
        raise ApiError(400, "bad_request", "missing card_id")
    assignee = data.get("assignee")
    if assignee is None:
        raise ApiError(
            400, "bad_request",
            "only 'assignee' is editable (title/body have no backing DB mutation)",
            "Field assignee is required (only the assignee is editable).",
        )
    _resolve_card_or_404(card_id)
    try:
        ok = bool(_backing_edit_card(card_id, assignee=assignee))
    except Exception as exc:
        raise ApiError(
            502, "edit_failed", str(exc), f"Card {card_id} could not be edited."
        )
    if not ok:
        return 200, {
            "id": str(card_id),
            "edited": False,
            "message": f"card {card_id} could not be edited",
        }
    return 200, {
        "id": str(card_id),
        "edited": True,
        "assignee": assignee,
        "message": f"edited card {card_id}",
    }


# --------------------------------------------------------------------------- #
# Route registration (import side-effect; loaded by api_server.py)
# --------------------------------------------------------------------------- #

ROUTES.append(("POST", re.compile(r"^/v1/cards$"), handle_create_card))
ROUTES.append(
    ("POST", re.compile(r"^/v1/review/(?P<card_id>[^/]+)/merge$"), handle_merge_card)
)
ROUTES.append(("POST", re.compile(r"^/v1/template/apply$"), handle_template_apply))
ROUTES.append(("POST", re.compile(r"^/v1/cluster/stop$"), handle_cluster_stop))
# Card-actions (mutating, confirm-gated). PATCH and the GET routes in
# routes_project share the same path shape but differ by HTTP method, so they
# coexist — api_server._dispatch matches on (method, path) together.
ROUTES.append(("POST", re.compile(r"^/v1/cards/(?P<card_id>[^/]+)/comment$"), handle_card_comment))
ROUTES.append(("POST", re.compile(r"^/v1/cards/(?P<card_id>[^/]+)/block$"), handle_card_block))
ROUTES.append(("POST", re.compile(r"^/v1/cards/(?P<card_id>[^/]+)/close$"), handle_card_close))
ROUTES.append(("PATCH", re.compile(r"^/v1/cards/(?P<card_id>[^/]+)$"), handle_card_edit))
