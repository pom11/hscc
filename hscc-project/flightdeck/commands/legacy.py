"""legacy.py — surface and safely re-home cards flightdeck cannot attribute.

Two commands:

  `flightdeck legacy-cards [--include-archived-boards]` — READ-ONLY. Surfaces
  every card that is NOT cleanly attributed to a registered project (an
  orphan board, or a workspace_path that resolves to no project), so a
  reviewer — a human in a Telegram topic, or the orchestrator driving through
  MCP — can decide which legacy cards are worth keeping and which project
  they really belong to. A suggestion is shown ONLY when workspace_path
  mechanically resolves to a project despite the wrong board; a card with no
  resolvable hint gets no suggestion. Flightdeck never guesses a project from
  title text alone — that judgment is deliberately not its job.

  `flightdeck migrate-card <card_id> --to <project> [--apply]` — mechanically,
  safely re-homes ONE card to the target project's board: creates a
  [migrated]-titled copy with a provenance line, archives the original
  (never deletes it) with a comment pointing at the new card, refuses an
  active (running/claimed) source card via reconcile's close-safety guard,
  and refuses an unknown target project by listing the known ones. Dry-run
  by default.

Everything external is injectable (cards, archived-board scan, kdb, the
registry); no test touches a real board or repo.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from ..core import kanban, registry

# Length of the body excerpt shown per card.
BODY_EXCERPT_CHARS = 200

# Title prefix so provenance is visible at a glance on a migrated card.
MIGRATED_PREFIX = "[migrated]"


def _excerpt(body: str | None, limit: int = BODY_EXCERPT_CHARS) -> str:
    """First ~``limit`` chars of a card body, collapsed to one line.

    A missing body is an empty string (never None, so JSON stays stable). The
    excerpt is whitespace-collapsed so a multi-line body is readable on one
    line; it is deliberately NOT smart about where it cuts — it does not judge
    content, only length.
    """
    if not body:
        return ""
    collapsed = " ".join(str(body).split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit] + "…"


def _registered_boards(projects: list[registry.Project]) -> set:
    """Board slugs that have a registry entry.

    A board is \"registered\" when at least one project maps to it via its
    ``board`` field. The set is used purely as a membership test: a board not
    in it is an orphan board whose cards need review.
    """
    return {str(p.board) for p in projects if getattr(p, "board", None)}


def _collect(
    cards: list[dict],
    projects: list[registry.Project],
) -> list[dict]:
    """The legacy rows: every card not cleanly attributed, with a suggestion.

    Each row is ``{id, board, status, title, body_excerpt, workspace_path,
    suggestion}`` where ``suggestion`` is the name of the registered project a
    card's ``workspace_path`` resolves to — or None when there is no resolvable
    hint (never guessed from title text). Attribution reuses
    ``kanban.project_for_card`` verbatim.
    """
    registered = _registered_boards(projects)
    rows: list[dict] = []
    for card in cards:
        board = str(card.get("board") or "")
        project = kanban.project_for_card(card, projects)
        unattributed = project is kanban.UNATTRIBUTED
        orphan_board = board not in registered
        if not (orphan_board or unattributed):
            continue  # cleanly attributed — not this command's concern
        suggestion = None if unattributed else str(project.name)
        rows.append(
            {
                "id": card.get("id"),
                "board": board,
                "status": card.get("status"),
                "title": card.get("title") or "(untitled)",
                "body_excerpt": _excerpt(card.get("body")),
                "workspace_path": card.get("workspace_path"),
                "suggestion": suggestion,
            }
        )
    return rows


def _group_by_board(rows: list[dict]) -> dict:
    """Group rows by board, ordering boards alphabetically and rows by id."""
    grouped: dict = {}
    for row in rows:
        grouped.setdefault(row["board"], []).append(row)
    for board_rows in grouped.values():
        board_rows.sort(key=lambda r: str(r["id"] or ""))
    return dict(sorted(grouped.items()))


def _render_grouped(grouped: dict) -> list[str]:
    """Text presentation, grouped by board so a reviewer scans one board at a time."""
    if not grouped:
        return ["no unmanaged cards — every card is on a registered board and attributed."]
    lines: list[str] = []
    for board, board_rows in grouped.items():
        lines.append(f"BOARD: {board}  ({len(board_rows)} card(s))")
        lines.append("-" * 60)
        for row in board_rows:
            lines.append(f"  id: {row['id'] or '?'}")
            lines.append(f"  status: {row['status'] or '?'}")
            lines.append(f"  title: {row['title']}")
            if row["workspace_path"]:
                lines.append(f"  workspace: {row['workspace_path']}")
            if row["suggestion"]:
                lines.append(f"  SUGGESTED TARGET PROJECT: {row['suggestion']}")
                lines.append(f"    (workspace resolves to {row['suggestion']} though the card sits on board '{board}')")
            else:
                lines.append(f"  suggestion: (none — no workspace_path resolves to a registered project)")
            if row["body_excerpt"]:
                lines.append(f"  body: {row['body_excerpt']}")
            lines.append("")
        lines.append("")
    return lines


def _render_json(grouped: dict) -> list[dict]:
    """Stable --json shape, grouped by board. Keys never reorder.

    A flat list of board groups, each ``{board, cards: [...]}``; each card is
    ``{id, status, title, body_excerpt, workspace_path, suggestion}``. Missing
    values stay stable (None / empty string). ``suggestion`` is None when there
    is no resolvable hint — the orchestrator never sees a guessed project.
    """
    out = []
    for board, board_rows in grouped.items():
        cards = [
            {
                "id": row["id"],
                "status": row["status"],
                "title": row["title"],
                "body_excerpt": row["body_excerpt"],
                "workspace_path": row["workspace_path"],
                "suggestion": row["suggestion"],
            }
            for row in board_rows
        ]
        out.append({"board": board, "cards": cards})
    return out


def cmd_legacy_cards(args: argparse.Namespace) -> int:
    """Compute and present the unmanaged-card review list. Returns exit code."""
    json_out = bool(getattr(args, "json", False))
    projects = registry.load_registry(args.registry)

    cards = getattr(args, "cards", None)
    if cards is None:
        try:
            cards = kanban.list_cards()
        except kanban.KanbanError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    include_archived = bool(getattr(args, "include_archived_boards", False))
    if include_archived:
        archived_reader = getattr(args, "list_archived_cards", None) or kanban.list_archived_board_cards
        archived = archived_reader(_kdb=getattr(args, "kdb", None))
        cards = list(cards) + list(archived)

    rows = _collect(cards, projects)
    grouped = _group_by_board(rows)

    if json_out:
        print(json.dumps(_render_json(grouped)))
        return 0
    for line in _render_grouped(grouped):
        print(line)
    return 0





def _get_project(projects: list[registry.Project], name: str):
    for proj in projects:
        if proj.name == name:
            return proj
    return None


def _active_reason(card: dict) -> str:
    """Why a running/claimed source card cannot be migrated.

    Reuses reconcile's close-safety "active card" guard — the card's status is
    inspected against :data:`kanban.ACTIVE_STATUSES` (running / claimed), the
    SAME set reconcile uses to never act on a card a worker is mid-way through.
    """
    status = str(card.get("status") or "")
    if status in kanban.ACTIVE_STATUSES:
        return (
            f"card {card.get('id')!r} is active (status={status}); "
            "refusing to migrate a card a worker may be mid-way through it."
        )
    return ""


def _build_migrated(card: dict) -> tuple[str, str]:
    """The new card's ``(title, body)`` with provenance.

    Title: the original prefixed ``[migrated]``. Body: the original with a
    prepended provenance line — ``Migrated from board '<source>' card <id> on
    <date>.`` — followed by a blank line and the original body (when it has
    one).
    """
    title = str(card.get("title") or "").strip()
    new_title = f"{MIGRATED_PREFIX} {title}".strip()

    source_board = str(card.get("board") or "?")
    provenance = (
        f"Migrated from board '{source_board}' card {card.get('id')} "
        f"on {date.today().isoformat()}."
    )
    body = card.get("body") or ""
    new_body = provenance if not body else f"{provenance}\n\n{body}"
    return new_title, new_body


def _print_plan(card: dict, board: str, new_title: str, new_body: str, project: str) -> None:
    """Print exactly what a migration WOULD do (dry-run path)."""
    print(f"migrate card {card.get('id')} -> project {project!r} (board {board!r}):")
    print(f"  CREATE on board {board!r}:")
    print(f"    title: {new_title}")
    if new_body:
        first_line = new_body.splitlines()[0]
        print(f"    body : {first_line}")
        print(f"           {new_body.splitlines()[1] if len(new_body.splitlines()) > 1 else ''}")
    print(f"  ARCHIVE original card {card.get('id')} with a note pointing at the new card (never deleted).")


def _archive_source(card: dict, new_id: str, *, _kdb) -> None:
    """Archive the ORIGINAL card with a comment pointing at the new card.

    Reuses Hermes' own ``archive_task`` (marks status ``archived`` — the schema
    already offers that; we never invent a status value) and ``add_comment``
    (the schema's note field) so the paper trail from old -> new survives
    archiving. The card is never deleted. ``_kdb`` is the injectable kanban
    library (a fake in tests).

    The source card may live on an archived board (found via ``find_card``'s
    archived fallback). Such a card carries ``_board_path`` — the archived
    board's db path, which cannot be resolved by ``connect(board=slug)`` — so
    connect through ``db_path`` directly when present; otherwise fall back to
    the live board slug.
    """
    board = card.get("board")
    if not board:
        raise kanban.KanbanError(
            f"card {card.get('id')} has no board; cannot archive it"
        )
    kdb = _kdb if _kdb is not None else kanban._load_kanban_db()
    board_path = card.get("_board_path")
    if board_path:
        conn = kdb.connect(db_path=Path(board_path))
    else:
        conn = kdb.connect(board=board)
    try:
        kdb.add_comment(
            conn,
            card.get("id"),
            "flightdeck-migrate",
            f"Migrated to card {new_id} on {date.today().isoformat()}.",
        )
        archived = kdb.archive_task(conn, card.get("id"))
    finally:
        conn.close()
    if not archived:
        # archive_task returns False (never raises) when the row did not match
        # — e.g. the original was already archived or vanished between
        # find_card (:312) and this archive. Mirror the review.py close_card
        # fix: surface that as a real failure so cmd_migrate_card reaches its
        # "migration is PARTIAL" path instead of printing "archived" + exit 0.
        raise kanban.KanbanError(
            f"card {card.get('id')} was not archived (already archived or "
            f"not found); the original is untouched"
        )


def cmd_migrate_card(args: argparse.Namespace, projects: list[registry.Project]) -> int:
    proj = _get_project(projects, args.to)
    if proj is None:
        known = sorted(p.name for p in projects)
        listing = ", ".join(known) if known else "(none registered)"
        print(
            f"error: unknown target project {args.to!r}; known projects: {listing}",
            file=sys.stderr,
        )
        return 2
    if not proj.repo:
        print(
            f"error: project {args.to} has no repo; cannot anchor a fresh worktree.",
            file=sys.stderr,
        )
        return 2

    _kdb = getattr(args, "kdb", None)
    card = kanban.find_card(args.card_id, _kdb=_kdb)
    if card is None:
        print(
            f"error: card {args.card_id!r} not found on any board "
            "(run `flightdeck why <card>` to confirm the id).",
            file=sys.stderr,
        )
        return 2

    # Safety: never migrate a card a worker could be mid-way through. Reuses
    # reconcile's active-card guard (kanban.ACTIVE_STATUSES).
    reason = _active_reason(card)
    if reason:
        print(f"error: {reason}", file=sys.stderr)
        return 2

    board, used_fallback = kanban.resolve_project_board(proj, _kdb=_kdb)
    if used_fallback:
        print(
            f"no board for {args.to}; would create on '{board}'",
            file=sys.stderr,
        )

    assignee = card.get("assignee")
    new_title, new_body = _build_migrated(card)
    # Anchor the new card in the target project's repo as a fresh worktree —
    # never the source board's stale one.
    workspace_path = proj.repo
    workspace_kind = "worktree"

    _print_plan(card, board, new_title, new_body, args.to)

    if not args.apply:
        print("dry-run: pass --apply to perform the migration.", file=sys.stderr)
        return 0

    # 1. CREATE the new card on the target board. A failure here means nothing
    #    was migrated.
    try:
        new_id = kanban.create_task(
            board,
            new_title,
            assignee=assignee,
            body=new_body,
            workspace_kind=workspace_kind,
            workspace_path=workspace_path,
            _kdb=_kdb,
        )
    except kanban.KanbanError as exc:
        print(
            f"error: could not create card on board {board!r}: {exc}",
            file=sys.stderr,
        )
        return 2

    # 2. Mark the ORIGINAL as migrated (archive + comment pointer). If this
    #    fails, the new card exists but the paper trail is broken — say so,
    #    print the new card id, and never report a clean success.
    try:
        _archive_source(card, new_id, _kdb=_kdb)
    except kanban.KanbanError as exc:
        print(
            f"error: card {new_id} was created on board {board!r} but archiving "
            f"the original {card.get('id')} FAILED: {exc}",
            file=sys.stderr,
        )
        print(f"new card id: {new_id}", file=sys.stderr)
        print("migration is PARTIAL — the copy exists but the original is untouched.", file=sys.stderr)
        return 1

    print(
        f"card {new_id} created on board {board!r}; original {card.get('id')} "
        f"archived with a pointer to {new_id}.",
    )
    return 0



def build_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "legacy-cards",
        help=(
            "unmanaged cards on orphan/archived boards for review "
            "(read-only, never mutates)"
        ),
        epilog="example: flightdeck legacy-cards --include-archived-boards",
    )
    p.add_argument(
        "--include-archived-boards",
        action="store_true",
        help=(
            "also scan Hermes' _archived/ board directories (read-only, "
            "not discovered by default — scanning archive history is expensive)"
        ),
    )
    p.set_defaults(func=cmd_legacy_cards)

    m = sub.add_parser(
        "migrate-card",
        help="re-home one card to the target project's board, safely",
        epilog="example: flightdeck migrate-card t_a1b2c3 --to flightdeck --apply",
    )
    m.add_argument("card_id", help="the card id to migrate")
    m.add_argument(
        "--to",
        required=True,
        metavar="PROJECT",
        help="target project name in the registry",
    )
    m.add_argument(
        "--apply",
        action="store_true",
        help="perform the migration (dry-run by default; nothing is created "
        "or archived without this)",
    )
    m.set_defaults(func=cmd_migrate_card)


def run(args: argparse.Namespace, registry_path: str) -> int:
    """Entry from cli.py. Dispatches to whichever subcommand matched.

    argparse's ``set_defaults(func=...)`` on each subparser already tells us
    which command was invoked; this only attaches the injectable seams each
    side's tests expect before delegating — never a reimplementation of
    either command's logic.
    """
    args.registry = registry_path

    # Real CLI/MCP callers always set .func via argparse's set_defaults or an
    # explicit MCP namespace. Unit tests exercising legacy-cards' run() often
    # build a bare Namespace with neither .func nor .card_id set, so the
    # missing case defaults to legacy-cards (this module's original,
    # single-command behaviour) rather than raising.
    func = getattr(args, "func", cmd_legacy_cards)
    if func is cmd_legacy_cards:
        args.json = getattr(args, "json", False)
        args.cards = getattr(args, "cards", None)
        args.list_archived_cards = getattr(args, "list_archived_cards", None)
        args.kdb = getattr(args, "kdb", None)
        args.include_archived_boards = getattr(args, "include_archived_boards", False)
        return cmd_legacy_cards(args)

    if func is cmd_migrate_card:
        args.kdb = getattr(args, "kdb", None)
        projects = registry.load_registry(registry_path)
        return cmd_migrate_card(args, projects)

    raise AssertionError(f"legacy.py run() called for an unknown subcommand: {args.func}")
