"""HSCC API — PROJECT / KANBAN read endpoints (Phase A3).

Read-only router for the flightdeck-backed (``hscc-project``) surface:
standup digest, cards list/detail, review queue + single-card review facts,
and the manual-QA queue. Every response carries a ``speak`` field (design §B).

Unlike the cluster endpoints (A2, ``routes_cluster.py``), these build their
payloads by calling flightdeck's *data* functions directly as libraries — the
same functions the ``hscc project`` CLI handlers use — rather than re-deriving
any logic here. We never shell out to ``hscc ...`` and parse text.

Conventions (design §A, shared):
  * handlers are ``(server, ctx, query, body) -> (status, payload_dict)``;
  * raise :class:`api_server.ApiError` for contract errors (404 unknown card);
  * a backing call that raises is caught and DEGRADED into a 200-with-honest-
    message (never a fabricated value, never a crash) unless a precise
    contract error (e.g. unknown card id -> 404) applies;
  * ``speak`` is ALWAYS present on a read response.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# Make the relocated flightdeck available exactly like
# ``hscc_daemon/hscc.py:_handle_project()`` does: insert ``hscc-project/`` on
# sys.path once, then import the modules directly. The plugin dir itself is put
# on sys.path by the plugin's conftest when tests run in isolation.
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
from flightdeck.core import registry as _registry        # noqa: E402
from flightdeck.core import review as _review_core       # noqa: E402
from flightdeck.core import git_state as _git_state      # noqa: E402
from flightdeck.commands import review as _review_cmd    # noqa: E402
from flightdeck.commands import qa as _qa_cmd            # noqa: E402
from flightdeck.commands import standup as _standup_cmd  # noqa: E402

from api_server import ApiError, ROUTES                  # noqa: E402

# The registry default ships with flightdeck (design §A: "the API server reads
# config for it"). Exposed for tests.
DEFAULT_REGISTRY = _registry.DEFAULT_REGISTRY

# Base branch the review facts merge against (matches flightdeck review).
_DEFAULT_BASE = "main"


# --------------------------------------------------------------------------- #
# Registry path resolution
# --------------------------------------------------------------------------- #

def _registry_path(ctx) -> str:
    """Resolve the flightdeck registry path for this server's config.

    Precedence (lowest -> highest): flightdeck default -> ``registry`` key in
    ``~/.hscc/api.json`` -> ``registry`` on the resolved config dict. Falls
    back to :data:`DEFAULT_REGISTRY` when nothing is configured, matching the
    CLI default so a fresh install reads the same boards as ``hscc project``.
    """
    from_config = getattr(ctx, "config", None)
    if from_config and from_config.get("registry"):
        return str(from_config["registry"])
    hscc_dir = getattr(ctx, "hscc_dir", None) or os.path.expanduser("~/.hscc")
    cfg_path = Path(hscc_dir) / "api.json"
    if cfg_path.exists():
        try:
            raw = json.loads(cfg_path.read_text())
            if isinstance(raw, dict) and raw.get("registry"):
                return str(raw["registry"])
        except (OSError, ValueError):
            pass  # fall through to the default
    return DEFAULT_REGISTRY


# --------------------------------------------------------------------------- #
# speak helpers (design §B) — pure, per-endpoint, derived from the data
# --------------------------------------------------------------------------- #

def _speak_standup(data: dict) -> str:
    """Headline over the non-empty sections; else 'Nothing needs attention.'"""
    n_review = len(data.get("needs_you") or [])
    n_running = len(data.get("running") or [])
    n_failing = len(data.get("failing") or [])
    clauses = []
    if n_review:
        clauses.append(f"{n_review} card{'s' if n_review != 1 else ''} need review")
    if n_running:
        clauses.append(f"{n_running} are running")
    if n_failing:
        clauses.append(f"{n_failing} failing")
    if not clauses:
        return "Nothing needs attention."
    return ", ".join(clauses) + "."


def _speak_cards(data: dict) -> str:
    count = len(data.get("cards") or [])
    running = sum(
        1 for c in (data.get("cards") or []) if str(c.get("status")) == "running"
    )
    if running:
        return f"{count} card{'s' if count != 1 else ''}, {running} running."
    return f"{count} card{'s' if count != 1 else ''}."


def _speak_card_detail(card: dict) -> str:
    cid = card.get("id") or "?"
    title = (card.get("title") or "(untitled)").strip()
    status = card.get("status") or "unknown"
    return f"Card {cid}: {title}. Status {status}."


def _speak_review_queue(data: dict) -> str:
    count = data.get("count") if isinstance(data.get("count"), int) else len(data.get("queue") or [])
    if count == 0:
        return "Nothing awaiting review."
    return f"{count} card{'s' if count != 1 else ''} await review."


def _speak_qa_queue(data: dict) -> str:
    queue_len = len(data.get("queue") or [])
    manual_len = len(data.get("manual_qa") or [])
    parts = []
    parts.append(f"{queue_len} card{'s' if queue_len != 1 else ''} need manual testing")
    if manual_len:
        parts.append(f"{manual_len} need manual verification")
    return ", ".join(parts) + "."


def _speak_review_detail(data: dict) -> str:
    """One clause: subject + merges-cleanly verdict."""
    cid = data.get("id") or "?"
    subject = data.get("subject")
    subject = (subject or "(unknown subject)").strip()
    conflicts = data.get("conflicts")
    if conflicts is None:
        merge_clause = "merge status unknown"
    elif conflicts == 0:
        merge_clause = "merges cleanly"
    else:
        n = int(conflicts)
        merge_clause = f"{n} conflict{'s' if n != 1 else ''} to resolve"
    return f"Card {cid} — {subject}, {merge_clause}."


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #

def handle_standup(server, ctx, query, body):
    """GET /v1/standup — the daily digest (standup.gather_data verbatim)."""
    registry_path = _registry_path(ctx)
    try:
        data = _standup_cmd.gather_data(registry_path)
    except Exception:
        data = {"error": "standup unavailable"}
    payload = dict(data or {})
    payload["speak"] = _speak_standup(payload) if not payload.get("error") else "Standup unavailable."
    return 200, payload


def handle_cards(server, ctx, query, body):
    """GET /v1/cards?board=...&status=... — cards across one/all boards."""
    board = query.get("board")
    include_archived = query.get("include_archived", "").lower() in ("1", "true", "yes")
    try:
        cards = _kanban.list_cards(board=board, include_archived=include_archived)
    except Exception:
        cards = []
        degraded = True
    else:
        degraded = False
    status = query.get("status")
    if not degraded and status:
        cards = [c for c in cards if str(c.get("status")) == status]
    payload = {"cards": cards or [], "count": len(cards or [])}
    payload["speak"] = (
        "Card list unavailable." if degraded else _speak_cards(payload)
    )
    return 200, payload


def handle_card_detail(server, ctx, query, body):
    """GET /v1/cards/{card_id} — one card's details (find_card)."""
    card_id = query.get("card_id")
    if card_id is None:
        raise ApiError(400, "bad_request", "missing card_id")
    try:
        card = _kanban.find_card(card_id)
    except Exception:
        card = None
    if card is None:
        raise ApiError(
            404, "not_found", f"no card with id {card_id!r}",
            f"Card {card_id} was not found.",
        )
    payload = dict(card)
    payload["speak"] = _speak_card_detail(card)
    return 200, payload


def handle_review_queue(server, ctx, query, body):
    """GET /v1/review/queue — cards genuinely awaiting review, newest first."""
    registry_path = _registry_path(ctx)
    try:
        projects = _registry.load_registry(registry_path)
        enriched = _review_cmd._enrich_project_cards(projects, _run=None)
        rows = _review_core.review_queue(enriched, now=None)
    except Exception:
        queue, degraded = [], True
        rows = []
    else:
        queue, degraded = rows, False
    payload = {"queue": queue, "count": len(queue)}
    payload["speak"] = (
        "Review queue unavailable." if degraded else _speak_review_queue(payload)
    )
    return 200, payload


def handle_review_detail(server, ctx, query, body):
    """GET /v1/review/{card_id} — DRY-RUN review facts (read-only, never mutates).

    Mirrors the ``cmd_review`` dry-run path (review.py:409-474): resolve the
    card, compute branch/merge facts, surface the VERIFY line. This endpoint
    NEVER merges and NEVER closes a card (that is A4's confirm-gated merge).
    A card that does not resolve -> 404.
    """
    card_id = query.get("card_id")
    if card_id is None:
        raise ApiError(400, "bad_request", "missing card_id")
    registry_path = _registry_path(ctx)
    try:
        cards = _kanban.list_cards()
        projects = _registry.load_registry(registry_path)
        card, project, branch = _review_cmd._resolve(cards, projects, card_id)
    except _review_cmd.ReviewError:
        # The card could not be resolved to a reviewable branch (not found, no
        # branch, or workspace_path unattributed) -> 404 per the design.
        raise ApiError(
            404, "not_found", f"card {card_id!r} does not resolve to a reviewable branch",
            f"Card {card_id} could not be resolved.",
        )
    # Any OTHER exception here (e.g. a malformed registry) is NOT a "card not
    # found" — let it propagate to the dispatcher's 500 internal_error handler.
    repo = project.repo
    # Dry-run only: read-only facts. No merge, no close, no mutation.
    landed = _review_cmd.git_state.is_merged(repo, branch, _DEFAULT_BASE)
    facts = _review_cmd._branch_facts(repo, branch, _DEFAULT_BASE)
    verify_present, verify_text = _review_cmd._verify_line(card.get("body"))
    payload = _review_cmd._render_json(
        card, project, branch, facts, verify_present, verify_text,
        projects=projects, landing=bool(landed),
    )
    payload["speak"] = _speak_review_detail(payload)
    return 200, payload


def handle_qa_queue(server, ctx, query, body):
    """GET /v1/qa/queue — the pre-merge QA queue + the manual-QA store."""
    registry_path = _registry_path(ctx)
    try:
        projects = _registry.load_registry(registry_path)
        cards = _kanban.list_cards()
        rows = _qa_cmd._collect(cards, projects, _run=None, _run_verify=None)
        manual = _qa_cmd._load_manual()
        unchecked = [e for e in manual if not e.get("checked")]
        unchecked.sort(key=lambda e: e.get("added_at") or "")
        payload_data = _qa_cmd._render_json(rows, unchecked)
    except Exception:
        payload_data = {"queue": [], "manual_qa": []}
        degraded = True
    else:
        degraded = False
    payload = dict(payload_data)
    payload["speak"] = (
        "QA queue unavailable." if degraded else _speak_qa_queue(payload)
    )
    return 200, payload


# --------------------------------------------------------------------------- #
# Projects — registry list / detail / scoped standup
# --------------------------------------------------------------------------- #

def _speak_projects_list(data: dict) -> str:
    """§B: "{n} project(s) registered."."""
    n = data.get("count", len(data.get("projects") or []))
    return f"{n} project{'s' if n != 1 else ''} registered."


def _speak_project_detail(data: dict) -> str:
    """§B: "{name}: {running} running, {open} open on board {board}."."""
    name = data.get("name") or "project"
    board = data.get("board_counts") or {}
    running = 0
    open_total = 0
    for status, cnt in board.items():
        if status == "running":
            running = cnt
        if status not in ("done", "archived", "blocked"):
            open_total += cnt
    return (f"{name}: {running} running, {open_total} open cards"
            f" on board {data.get('board') or 'unknown'}.")


def _speak_project_standup(data: dict) -> str:
    """§B: mirror the unscoped standup headline for the scoped digest."""
    n_review = len(data.get("needs_you") or [])
    n_running = len(data.get("running") or [])
    n_failing = len(data.get("failing") or [])
    clauses = []
    if n_review:
        clauses.append(f"{n_review} card{'s' if n_review != 1 else ''} need review")
    if n_running:
        clauses.append(f"{n_running} are running")
    if n_failing:
        clauses.append(f"{n_failing} failing")
    if not clauses:
        return "Nothing needs attention."
    return ", ".join(clauses) + "."


def _project_detail(projects, proj):
    """Compute per-project detail: board counts, git state, last activity.

    All reads are wrapped defensively — a missing repo or an unreadable board
    degrades to honest None/[] rather than crashing the endpoint. Git facts
    come from ``git_state`` (real subprocess on the live path, stubbed in
    tests); board counts come from ``kanban.list_cards``.
    """
    board = getattr(proj, "board", None)
    board_counts = {}
    if board:
        try:
            cards = _kanban.list_cards(board=board)
            for c in cards:
                st = str(c.get("status") or "unknown")
                board_counts[st] = board_counts.get(st, 0) + 1
            board_counts["total"] = len(cards)
        except Exception:
            board_counts = {"total": 0}

    repo = getattr(proj, "repo", None)
    git = {"is_repo": False}
    if repo:
        try:
            git["is_repo"] = bool(_git_state.is_repo(repo))
            if git["is_repo"]:
                git["branch"] = _git_state.current_branch(repo)
                git["dirty"] = bool(_git_state.is_dirty(repo))
                git["uncommitted"] = _git_state.uncommitted_files(repo)
                last_age = _git_state.last_commit_age_seconds(repo)
                git["last_activity_seconds_ago"] = last_age
                git["head"] = _git_state.head_sha(repo)
        except Exception:
            git = {"is_repo": bool(git.get("is_repo"))}

    return {"board_counts": board_counts, "git": git}


def handle_projects(server, ctx, query, body):
    """GET /v1/projects — the registry list (name, repo, board, topic)."""
    registry_path = _registry_path(ctx)
    try:
        projects = _registry.load_registry(registry_path)
    except Exception:
        return 200, {"speak": "Project list unavailable."}
    rows = [
        {
            "name": getattr(p, "name", None),
            "repo": getattr(p, "repo", None),
            "board": getattr(p, "board", None) or "unknown",
            "topic": getattr(p, "topic", None) if getattr(p, "topic", None) is not None else "unknown",
        }
        for p in projects
    ]
    payload = {"projects": rows, "count": len(rows)}
    payload["speak"] = _speak_projects_list(payload)
    return 200, payload


def handle_project_detail(server, ctx, query, body):
    """GET /v1/projects/{name} — per-project detail (board counts, git state, activity)."""
    name = query.get("name")
    if name is None or not str(name).strip():
        raise ApiError(400, "bad_request", "missing project name")
    registry_path = _registry_path(ctx)
    try:
        projects = _registry.load_registry(registry_path)
        proj = _registry.get_project(name, path=registry_path)
    except _registry.ProjectNotFoundError:
        raise ApiError(
            404, "not_found", f"no project named {name!r}",
            f"Project {name} was not found.",
        )
    except Exception:
        return 200, {"speak": "Project detail unavailable."}
    detail = _project_detail(projects, proj)
    payload = {
        "name": getattr(proj, "name", None) or name,
        "repo": getattr(proj, "repo", None),
        "board": getattr(proj, "board", None) or "unknown",
        "topic": getattr(proj, "topic", None) if getattr(proj, "topic", None) is not None else "unknown",
        "board_counts": detail["board_counts"],
        "git": detail["git"],
    }
    payload["speak"] = _speak_project_detail(payload)
    return 200, payload


def handle_project_standup(server, ctx, query, body):
    """GET /v1/projects/{name}/standup — project-scoped standup digest.

    ``standup.gather_data`` walks every registered project in one pass (it
    does not itself support per-project scoping), so this endpoint runs the
    full digest and FILTERS the card rows down to this project's cards, using
    the same ``kanban.project_for_card`` attribution the digest itself uses.
    Cards that cannot be attributed to this project are dropped; the ``running``
    UNATTRIBUTED bucket is omitted for the scoped view.
    """
    name = query.get("name")
    if name is None or not str(name).strip():
        raise ApiError(400, "bad_request", "missing project name")
    registry_path = _registry_path(ctx)
    try:
        projects = _registry.load_registry(registry_path)
        _registry.get_project(name, path=registry_path)
    except _registry.ProjectNotFoundError:
        raise ApiError(
            404, "not_found", f"no project named {name!r}",
            f"Project {name} was not found.",
        )
    except Exception:
        return 200, {"speak": "Standup unavailable."}
    try:
        data = _standup_cmd.gather_data(registry_path)
    except Exception:
        return 200, {"speak": "Standup unavailable."}
    if not isinstance(data, dict):
        return 200, {"speak": "Standup unavailable."}

    def _belongs(card):
        try:
            proj = _kanban.project_for_card(card, projects)
            return proj is not None and getattr(proj, "name", None) == name
        except Exception:
            return False

    payload = {}
    for section in ("needs_you", "running", "stale", "failing", "drift",
                    "unreadable"):
        rows = data.get(section) or []
        if section == "running":
            payload[section] = [r for r in rows if _belongs(r)]
        else:
            # Non-card-only sections (drift/unreadable) are per-project rows
            # keyed by project name; keep the row whose key matches.
            payload[section] = [r for r in rows
                                if (r.get("project") if isinstance(r, dict)
                                    else None) == name
                                or _belongs(r)]
    payload["coverage"] = data.get("coverage")
    payload["speak"] = _speak_project_standup(payload)
    return 200, payload


# --------------------------------------------------------------------------- #
# Route registration
# --------------------------------------------------------------------------- #

ROUTES.append(("GET", re.compile(r"^/v1/standup$"), handle_standup))
ROUTES.append(("GET", re.compile(r"^/v1/cards$"), handle_cards))
ROUTES.append(("GET", re.compile(r"^/v1/cards/(?P<card_id>[^/]+)$"), handle_card_detail))
ROUTES.append(("GET", re.compile(r"^/v1/review/queue$"), handle_review_queue))
ROUTES.append(("GET", re.compile(r"^/v1/review/(?P<card_id>[^/]+)$"), handle_review_detail))
ROUTES.append(("GET", re.compile(r"^/v1/qa/queue$"), handle_qa_queue))
ROUTES.append(("GET", re.compile(r"^/v1/projects$"), handle_projects))
ROUTES.append(("GET", re.compile(r"^/v1/projects/(?P<name>[^/]+)$"), handle_project_detail))
ROUTES.append(("GET", re.compile(r"^/v1/projects/(?P<name>[^/]+)/standup$"), handle_project_standup))
