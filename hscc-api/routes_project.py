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
from flightdeck.core import release as _release_core     # noqa: E402
from flightdeck.core import roadmap as _roadmap_core     # noqa: E402
from flightdeck.commands import review as _review_cmd    # noqa: E402
from flightdeck.commands import qa as _qa_cmd            # noqa: E402
from flightdeck.commands import standup as _standup_cmd  # noqa: E402
from flightdeck.commands import roadmap as _roadmap_cmd  # noqa: E402
from flightdeck.commands import metrics as _metrics_cmd  # noqa: E402
from flightdeck.commands import hygiene as _hygiene_cmd  # noqa: E402
from flightdeck.commands import why as _why_cmd          # noqa: E402

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
                branch = git["branch"]
                if branch:
                    # Push/pull sync signals vs the branch's tracking upstream.
                    # The project-overview card wants ahead/behind so the
                    # operator sees how far local is from the remote. Both are
                    # defensive (0 when no upstream / non-repo — safe readings).
                    git["ahead"] = _git_state.ahead_of_upstream(repo, branch)
                    git["behind"] = _git_state.behind_of_upstream(repo, branch)
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
    # Session-health surfacing (t_a8e9b7ff Bug 1): report the project chat
    # session's real bloat signals so the operator can see it approaching the
    # context ceiling BEFORE it wedges. The chat session for ``<name>`` lives on
    # the ``<name>-orch`` profile as a session titled ``<name>`` (the
    # orchestrators resolver convention). Lazy import (avoids a module-level
    # cycle — routes_orchestrator imports ``_registry_path`` from this module);
    # any failure degrades to omitting the field, never failing the request.
    try:
        from routes_orchestrator import _session_health
        proj_name = getattr(proj, "name", None) or name
        health = _session_health(
            ctx, f"{proj_name}-orch", str(proj_name))
    except Exception:
        health = None
    if health is not None:
        payload["session_health"] = health
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
# Why — one card's full story (kanban + git), the antidote to confident invention
# --------------------------------------------------------------------------- #

def _speak_why(data: dict) -> str:
    """§B: lead with the verdict, the one line that says what's going on."""
    verdict = data.get("verdict")
    title = data.get("title")
    if not verdict:
        return f"No stance available for {title or 'this card'}."
    return f"{title or 'This card'}: {verdict}"


def handle_why(server, ctx, query, body):
    """GET /v1/why/{card_id} — the card's full story (kanban + git facts).

    Read-only assembly of ``flightdeck why``: identity, timing, branch,
    workspace, commits and the one-line ``verdict`` on what would move the
    card. The verdict is the "antidote to an agent that confidently invents
    things" — every claim traces to a checked git fact or card event, never a
    guess.
    """
    card_id = query.get("card_id")
    if card_id is None or not str(card_id).strip():
        raise ApiError(400, "bad_request", "missing card id")
    registry_path = _registry_path(ctx)
    try:
        projects = _registry.load_registry(registry_path)
    except Exception:
        return 200, {"speak": "Card story unavailable."}
    try:
        story = _why_cmd.gather(str(card_id), projects)
    except _why_cmd.UnknownCardError:
        raise ApiError(
            404, "not_found", f"no card with id {card_id!r}",
            f"Card {card_id} was not found.",
        )
    except Exception:
        return 200, {"speak": "Card story unavailable."}
    payload = _why_cmd.render_json(story)
    payload["speak"] = _speak_why(payload)
    return 200, payload


# --------------------------------------------------------------------------- #
# Portfolio surfaces — project-scoped read endpoints (roadmap / incidents /
# release / metrics / hygiene)
# --------------------------------------------------------------------------- #

def _resolve_project(name, registry_path):
    """Resolve a single project by registry name, or raise the 404 ApiError."""
    try:
        projects = _registry.load_registry(registry_path)
        proj = _registry.get_project(name, path=registry_path)
        return projects, proj
    except _registry.ProjectNotFoundError:
        raise ApiError(
            404, "not_found", f"no project named {name!r}",
            f"Project {name} was not found.",
        )


def _speak_roadmap(data: dict) -> str:
    """§B: project roadmap — milestones present/done summary."""
    name = data.get("name") or "project"
    if not data.get("present"):
        return f"{name} has no roadmap."
    sections = data.get("milestones") or {}
    built = []
    for sec, ms in sections.items():
        if ms.get("total"):
            built.append(f"{sec} {ms.get('done', 0)}/{ms.get('total', 0)}")
    return f"{name} roadmap: " + (", ".join(built) if built else "no items yet.")


def handle_project_roadmap(server, ctx, query, body):
    """GET /v1/projects/{name}/roadmap — the project's ROADMAP.md milestones.

    Surfaces the show view (Now/Next/Later checklist items with done/total) via
    ``roadmap.parse_roadmap``. Read-only; a missing ROADMAP.md reports
    ``present:false`` rather than an error.
    """
    name = query.get("name")
    if name is None or not str(name).strip():
        raise ApiError(400, "bad_request", "missing project name")
    registry_path = _registry_path(ctx)
    try:
        _projects, proj = _resolve_project(name, registry_path)
    except ApiError:
        raise
    except Exception:
        return 200, {"speak": "Roadmap unavailable."}
    try:
        path = _roadmap_cmd._definite_path(proj)
        r = _roadmap_core.parse_roadmap(path)
    except Exception:
        return 200, {"name": name, "present": False,
                     "speak": "Roadmap unavailable."}
    milestones = {}
    if r.present:
        for section in _roadmap_cmd._SECTION_HEADING.values():
            m = r.milestone(section)
            milestones[section] = {
                "items": [
                    {"text": it.text, "checked": it.checked}
                    for it in (m.items if m else [])
                ],
                "done": m.done_count if m else 0,
                "total": m.total if m else 0,
            }
    payload = {
        "name": name,
        "present": r.present,
        "path": path,
        "milestones": milestones,
    }
    payload["speak"] = _speak_roadmap(payload)
    return 200, payload


def _parse_incidents(text: str) -> list:
    """Parse ``docs/INCIDENTS.md`` text into newest-first entry dicts.

    Each entry is the ``## YYYY-MM-DD — heading`` block with the five ``**k:**``
    fields. Entries are returned in file order (newest first, the file's own
    convention). A block missing a field renders that field as ``""``; the file
    header (text before the first ``## ``) is dropped.
    """
    entries = []
    current = None
    field_re = re.compile(r"^\*\*([^*]+):\*\*\s*(.*)$")
    for line in text.splitlines():
        if line.startswith("## "):
            if current is not None:
                entries.append(current)
            rest = line[3:].strip()
            parts = rest.split("—", 1)
            date = parts[0].strip()
            heading = parts[1].strip() if len(parts) > 1 else ""
            current = {
                "date": date, "heading": heading, "project": "",
                "symptom": "", "cause": "", "fix": "", "lesson": "",
            }
            continue
        if current is None:
            continue  # header block
        m = field_re.match(line)
        if m:
            current[m.group(1).strip().lower()] = m.group(2).strip()
    if current is not None:
        entries.append(current)
    return entries


def _speak_incidents(data: dict) -> str:
    """§B: project incidents — count of recorded lessons."""
    name = data.get("name") or "project"
    if not data.get("present"):
        return f"{name} has no incident log."
    n = len(data.get("incidents") or [])
    return f"{name}: {n} recorded incident{'s' if n != 1 else ''}."


def handle_project_incidents(server, ctx, query, body):
    """GET /v1/projects/{name}/incidents — the project's docs/INCIDENTS.md log.

    Read-only parse of the newest-first lesson log into structured entries
    (date, heading, symptom, cause, fix, lesson). A missing/unreadable log
    reports ``present:false`` rather than an error.
    """
    name = query.get("name")
    if name is None or not str(name).strip():
        raise ApiError(400, "bad_request", "missing project name")
    registry_path = _registry_path(ctx)
    try:
        _projects, proj = _resolve_project(name, registry_path)
    except ApiError:
        raise
    except Exception:
        return 200, {"speak": "Incidents unavailable."}
    path = os.path.join(
        getattr(proj, "repo", "") or "",
        os.path.join("docs", "INCIDENTS.md"),
    )
    present = False
    entries = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        entries = _parse_incidents(text)
        present = True
    except (OSError, IOError, UnicodeDecodeError):
        present = False
    payload = {"name": name, "present": present, "path": path,
               "incidents": entries}
    payload["speak"] = _speak_incidents(payload)
    return 200, payload


_RELEASE_PLAN_STEPS = [
    "bump VERSION",
    "commit the version bump",
    "tag the release (annotated)",
    "push the branch and tag",
    "create the GitHub release",
    "install the release",
    "verify the installed version is live",
]


def _speak_release(data: dict) -> str:
    """§B: release readiness — ready, or first blocker."""
    name = data.get("name") or "project"
    problems = data.get("problems") or []
    if data.get("ready"):
        return (f"{name} is release-ready for {data.get('version') or '?'}: "
                f"{len(data.get('plan') or [])} dry-run steps would run.")
    if problems:
        return f"{name} is NOT release-ready for {data.get('version') or '?'}: {problems[0]['code']}."
    return f"{name} release readiness unavailable."


def handle_project_release(server, ctx, query, body):
    """GET /v1/projects/{name}/release?version=X — dry-run release readiness.

    Read-only: runs ``release.preconditions`` and reports every blocker plus
    the ordered plan a real release would execute. Never applies, never mutates.
    ``version`` is required (the target being checked); a missing version is a
    400.
    """
    name = query.get("name")
    version = query.get("version")
    if name is None or not str(name).strip():
        raise ApiError(400, "bad_request", "missing project name")
    if version is None or not str(version).strip():
        raise ApiError(400, "bad_request", "missing version")
    registry_path = _registry_path(ctx)
    try:
        _projects, proj = _resolve_project(name, registry_path)
    except ApiError:
        raise
    except Exception:
        return 200, {"speak": "Release readiness unavailable."}
    try:
        problems = _release_core.preconditions(proj, str(version))
    except Exception:
        return 200, {"name": name, "ready": False, "problems": [],
                     "speak": "Release readiness unavailable."}
    payload = {
        "name": name,
        "version": str(version),
        "ready": not problems,
        "problems": [{"code": p.code, "message": p.message} for p in problems],
        "plan": _RELEASE_PLAN_STEPS,
    }
    payload["speak"] = _speak_release(payload)
    return 200, payload


def _speak_metrics(data: dict) -> str:
    """§B: metrics headline — reviewed/merged in the window."""
    name = data.get("name") or "project"
    if data.get("metrics") is None:
        return f"{name} metrics unavailable."
    m = data["metrics"]
    return (f"{name}: {m.get('merged_count', 0)} merged, "
            f"{m.get('reviewed', 0)} reviewed in the window.")


def handle_project_metrics(server, ctx, query, body):
    """GET /v1/projects/{name}/metrics — project-scoped quality metrics.

    Read-only via ``metrics.gather(project=<name>)``: first-time-pass,
    stalled rate, review latency, throughput and rework over the default 24h
    window. An unknown project is a 404; degraded reads report honest None
    figures rather than fabricated values.
    """
    name = query.get("name")
    if name is None or not str(name).strip():
        raise ApiError(400, "bad_request", "missing project name")
    registry_path = _registry_path(ctx)
    try:
        projects, _proj = _resolve_project(name, registry_path)
    except ApiError:
        raise
    except Exception:
        return 200, {"speak": "Metrics unavailable."}
    try:
        now_ts = float(__import__("time").time())
        since_ts = now_ts - _metrics_cmd.DEFAULT_SINCE_SECONDS
        metrics_dict = _metrics_cmd.gather(
            projects, since_ts=since_ts, now=now_ts, project=name,
        )
    except Exception:
        return 200, {"name": name, "metrics": None,
                     "speak": "Metrics unavailable."}
    payload = {
        "name": name,
        "window_days": metrics_dict["window"]["days"],
        "metrics": _metrics_cmd.render_json(metrics_dict),
    }
    payload["speak"] = _speak_metrics(payload)
    payload["metrics"]["window"] = {
        "since": metrics_dict["window"]["since"],
        "now": metrics_dict["window"]["now"],
        "days": metrics_dict["window"]["days"],
    }
    return 200, payload


def _speak_hygiene(data: dict) -> str:
    """§B: per-project hygiene — clean, or the issue count."""
    name = data.get("name") or "project"
    n = data.get("issue_count", 0)
    if n == 0:
        return f"{name} hygiene: clean."
    return f"{name} hygiene: {n} issue{'s' if n != 1 else ''}."


def handle_project_hygiene(server, ctx, query, body):
    """GET /v1/projects/{name}/hygiene — board-decay findings for this project.

    Read-only. Hygiene is detected board-wide (duplicate cards, triage traps,
    stale worktrees), then filtered to this project by attributing each card to
    a project via ``kanban.project_for_card`` and each stale worktree to the
    repo it lives under. A fully clean project reports ``issue_count: 0``.
    """
    name = query.get("name")
    if name is None or not str(name).strip():
        raise ApiError(400, "bad_request", "missing project name")
    registry_path = _registry_path(ctx)
    try:
        projects, _proj = _resolve_project(name, registry_path)
    except ApiError:
        raise
    except Exception:
        return 200, {"speak": "Hygiene unavailable."}
    try:
        all_cards = _kanban.list_cards(board=None, include_archived=True)
        worktrees = _hygiene_cmd._collect_worktrees(projects)
        active = [c for c in all_cards
                  if str(c.get("status") or "") != "archived"]
        closed_ids = {
            c["id"] for c in all_cards
            if str(c.get("status") or "") in _hygiene_cmd.hygiene.CLOSED_STATUSES
        }
        worktree_ids = {w["card_id"] for w in worktrees}
        need_facts = (
            {c["id"] for c in active
             if str(c.get("status") or "") == _hygiene_cmd.hygiene.TRIAGE_STATUS}
            | worktree_ids
        )
        git_facts = _hygiene_cmd._git_facts_for_cards(
            all_cards, projects, card_ids=need_facts,
        )
        plan = _hygiene_cmd.hygiene.build_plan(
            active, git_facts, worktrees, closed_ids,
            threshold=_hygiene_cmd.hygiene.DEFAULT_SIMILARITY,
        )
    except Exception:
        return 200, {"name": name, "issue_count": 0,
                     "speak": "Hygiene unavailable."}

    def _card_project(card_id, board=None):
        for c in all_cards:
            if c.get("id") == card_id:
                try:
                    p = _kanban.project_for_card(c, projects)
                    if p is not _kanban.UNATTRIBUTED:
                        return getattr(p, "name", None)
                except Exception:
                    return None
                return None
        return None

    def _worktree_project(wt):
        try:
            for p in projects:
                repo = getattr(p, "repo", None)
                if repo and wt.get("worktree", "").startswith(
                        repo + os.sep):
                    return getattr(p, "name", None)
            return None
        except Exception:
            return None

    def _in_project(proj_name):
        return proj_name is not None and proj_name == name

    duplicates = [
        {"board": d.get("board"), "title": d.get("title"),
         "keep": d.get("keep", {}).get("id"),
         "archive": [c["id"] for c in (d.get("archive") or [])]}
        for d in plan["duplicates"]
        if _in_project(_card_project(d.get("keep", {}).get("id")))
    ]
    triage = [
        {"board": r["card"]["board"], "card_id": r["card"]["id"],
         "title": r["card"]["title"], "branch": r["branch"],
         "branch_has_work": r["branch_has_work"],
         "commits_ahead": r["commits_ahead"]}
        for r in plan["triage"]
        if _in_project(_card_project(r["card"]["id"]))
    ]
    stale_worktrees = [
        {"card_id": s["card_id"], "board": s["board"],
         "worktree": s["worktree"]}
        for s in plan["stale_worktrees"]
        if _in_project(_worktree_project(s))
    ]
    payload = {
        "name": name,
        "present": True,
        "duplicates": duplicates,
        "triage": triage,
        "stale_worktrees": stale_worktrees,
        "issue_count": len(duplicates) + len(triage) + len(stale_worktrees),
    }
    payload["speak"] = _speak_hygiene(payload)
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
ROUTES.append(("GET", re.compile(r"^/v1/why/(?P<card_id>[^/]+)$"), handle_why))
ROUTES.append(("GET", re.compile(r"^/v1/projects/(?P<name>[^/]+)/roadmap$"), handle_project_roadmap))
ROUTES.append(("GET", re.compile(r"^/v1/projects/(?P<name>[^/]+)/incidents$"), handle_project_incidents))
ROUTES.append(("GET", re.compile(r"^/v1/projects/(?P<name>[^/]+)/release$"), handle_project_release))
ROUTES.append(("GET", re.compile(r"^/v1/projects/(?P<name>[^/]+)/metrics$"), handle_project_metrics))
ROUTES.append(("GET", re.compile(r"^/v1/projects/(?P<name>[^/]+)/hygiene$"), handle_project_hygiene))
