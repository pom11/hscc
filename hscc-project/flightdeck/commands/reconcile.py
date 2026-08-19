"""reconcile.py — `flightdeck reconcile` : the board/git integrity fix.

For every card whose board maps to a repo in the registry, decide what the
board should look like given what is true about git, and --apply the changes:

  - branch is an ancestor of ``main``  -> CLOSE the card (work landed). This is
    the missing transition that let 28 cards look like a backlog when zero
    needed attention — 14 had already-merged branches.
  - no branch, no commits, older than N days -> flag for ARCHIVE (dead work).
  - branch exists, unmerged, zero commits -> flag STALE (in flight but stuck).

Dry-run by default: the command prints exactly the plan it would execute and
touches nothing. ``--apply`` performs the closes and archives, one card at a
time, and always prints the plan first.

The classification rules live in :mod:`flightdeck.core.kanban` (pure decision
logic, already unit-tested). This module only gathers inputs — cards, git
facts via :mod:`flightdeck.core.git_state`, and the board/repo mapping from the
registry — presents the plan, and acts on it through injectable handles.

Safety rule (the "never report unverified" principle): a card on a board with
no mapped repo cannot be checked against git, so it is excluded from the plan
entirely rather than guessed at. We never archive what we could not verify.
"""

from __future__ import annotations

import argparse
import sys

from ..core import git_state, kanban, registry

# Stale / dead thresholds are owned by kanban.core (single source of truth);
# the command lets the operator override --days from the CLI.


def _git_facts_for_cards(
    cards: list[dict],
    projects: list[registry.Project],
    *,
    _run=None,
) -> dict:
    """{card_id: {branch_exists, is_merged, commits_ahead, own_commits, landed_via_merge}} via git_state.

    A card's repo is resolved by its ``workspace_path`` attribution
    (:func:`kanban.project_for_card`), NOT by its board — the shared board holds
    cards from many projects. Only cards that resolve to a project get an entry;
    a card that resolves to no project (UNATTRIBUTED) is left out so downstream
    reconciliation cannot claim anything it never verified.
    """
    facts: dict[str, dict] = {}
    for card in cards:
        cid = card.get("id")
        branch = card.get("branch")
        if not cid or not branch:
            continue
        project = kanban.project_for_card(card, projects)
        if project is kanban.UNATTRIBUTED:
            continue  # no resolved repo -> cannot verify against git
        repo = project.repo
        exists = git_state.branch_exists(repo, branch, _run=_run)
        merged = git_state.is_merged(repo, branch, _run=_run) if exists else False
        ahead = git_state.commits_ahead(repo, branch, _run=_run) if exists else 0
        own = git_state.own_commits(repo, branch, _run=_run) if exists else 0
        landed = (
            git_state.landed_via_merge(repo, branch, _run=_run) if exists else False
        )
        facts[cid] = {
            "branch_exists": exists,
            "is_merged": merged,
            "commits_ahead": ahead,
            "own_commits": own,
            "landed_via_merge": landed,
        }
    return facts


def _verifiable_cards(
    cards: list[dict], projects: list[registry.Project]
) -> list[dict]:
    """Cards that resolve to a project repo via workspace_path attribution — the
    only ones we can reconcile. A card that resolves to no project is
    UNATTRIBUTED and is excluded: it cannot be verified against git, so we never
    act on it.
    """
    return [
        c
        for c in cards
        if kanban.project_for_card(c, projects) is not kanban.UNATTRIBUTED
    ]


# --------------------------------------------------------------------------- #
# presentation
# --------------------------------------------------------------------------- #


def _render(plan: dict, *, dead_days: int = kanban.DEFAULT_DEAD_DAYS) -> list[str]:
    lines: list[str] = []
    for section, label in (
        ("close", "CLOSE (work landed)"),
        ("archive", f"ARCHIVE (dead, unstarted > {dead_days} days)"),
        ("stale", "STALE (in flight, zero commits)"),
        ("skipped", "SKIPPED (could not confirm work landed)"),
    ):
        entries = plan.get(section, [])
        lines.append(f"{label} ({len(entries)})")
        if not entries:
            lines.append("  none")
        for item in entries:
            if isinstance(item, dict):
                reason = f"  — {item['reason']}" if item.get("reason") else ""
                lines.append(f"  {item['id']}  {item.get('title')!r}{reason}")
            else:
                lines.append(f"  {item}")
        lines.append("")
    return lines


def _render_json(plan: dict) -> dict:
    out: dict[str, list] = {"close": [], "archive": [], "stale": [], "skipped": []}
    for section, entries in plan.items():
        for item in entries:
            if isinstance(item, dict):
                row = {
                    "card_id": item["id"],
                    "title": item.get("title"),
                    "board": item.get("board"),
                }
                if item.get("reason"):
                    row["reason"] = item["reason"]
                out[section].append(row)
            else:
                out[section].append({"card_id": item})
    return out


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #


def build_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "reconcile",
        help="close cards whose branch merged; flag dead/stale cards",
        epilog="example: flightdeck reconcile --apply",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="perform the closes (dry-run by default)",
    )
    p.add_argument(
        "--days",
        type=int,
        default=kanban.DEFAULT_DEAD_DAYS,
        metavar="N",
        help="dead when no branch/no commits and older than N days "
        f"(default: {kanban.DEFAULT_DEAD_DAYS})",
    )
    p.set_defaults(func=cmd_reconcile)


def cmd_reconcile(args: argparse.Namespace, projects: list[registry.Project]) -> int:
    all_cards = kanban.list_cards(board=None, include_archived=False)
    verifiable = _verifiable_cards(all_cards, projects)
    git_facts = _git_facts_for_cards(verifiable, projects, _run=args.run)

    plan = kanban.reconcile_plan(
        verifiable,
        git_facts,
        dead_days=args.days,
    )

    # Build a display-ready plan annotated with card titles/boards so the
    # human can see exactly what will happen. Only ids are acted on later.
    display: dict[str, list[dict]] = {"close": [], "archive": [], "stale": []}
    by_id = {c["id"]: c for c in verifiable}
    for section in ("close", "archive", "stale"):
        for cid in plan.get(section, []):
            c = by_id.get(cid, {})
            display[section].append(
                {"id": cid, "title": c.get("title"), "board": c.get("board")}
            )

    if args.json:
        import json

        print(json.dumps(_render_json(display)))
    else:
        for line in _render(display):
            print(line)

    if args.apply:
        summary = _apply_plan(plan, by_id, _kdb=args.kdb)
        print(
            f"applied: closed {len(summary['closed'])} card(s), "
            f"archived {len(summary['archived'])} card(s).",
            file=sys.stderr,
        )
    else:
        print("dry-run: pass --apply to perform these closes.", file=sys.stderr)
    return 0


def _default_kdb():
    return kanban._load_kanban_db()


def _apply_plan(plan: dict, by_id: dict, *, _kdb=None) -> dict:
    """Perform close + archive on the cards named in the plan.

    ``_kdb`` is the hermes kanban_db module (or a stand-in exposing
    ``connect``, ``complete_task``, ``archive_task``). Close uses Hermes'
    ``complete_task``; archive uses ``archive_task``. Each is idempotent at the
    item level (both return False for a card already in the target state), so
    re-running never double-writes.

    ``stale`` entries are deliberately NOT acted on: a stale card is in flight
    (claimed/running) and archiving or closing it could destroy live work. Stale
    is a report-only flag, surfaced to the operator, never a mutation.
    """
    kdb = _kdb if _kdb is not None else _default_kdb()
    summary: dict = {"closed": [], "archived": []}

    boards = {c["board"]: c for c in by_id.values() if c.get("board")}
    for cid in plan.get("close", []):
        board = by_id.get(cid, {}).get("board")
        if not board:
            continue
        conn = kdb.connect(board=board)
        try:
            if kdb.complete_task(conn, cid, result="closed by flightdeck reconcile — branch merged into main"):
                summary["closed"].append(cid)
        finally:
            conn.close()

    for cid in plan.get("archive", []):
        board = by_id.get(cid, {}).get("board")
        if not board:
            continue
        conn = kdb.connect(board=board)
        try:
            if kdb.archive_task(conn, cid):
                summary["archived"].append(cid)
        finally:
            conn.close()

    return summary


def run(args: argparse.Namespace, registry_path: str) -> int:
    """Entry from cli.py: run reconcile with injectable handles attached.

    ``args.kdb`` / ``args.run`` default to None (use the real kanban library /
    real git); tests set them to fakes so nothing here touches a live board or
    repo.
    """
    args.registry = registry_path
    args.kdb = getattr(args, "kdb", None)
    args.run = getattr(args, "run", None)
    projects = registry.load_registry(registry_path)
    return cmd_reconcile(args, projects)
