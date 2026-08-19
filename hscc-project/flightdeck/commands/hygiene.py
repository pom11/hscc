"""hygiene.py — `flightdeck hygiene` : board decay report + one-shot fix.

Detects three real decay modes across the boards flightdeck can see:

  - DUPLICATES      near-identical titles on one board -> archive all but newest
  - TRIAGE TRAP     cards stuck in the triage column    -> archive + recreate
                    preserving the branch
  - STALE WORKTREES .worktrees/<card> whose card is closed AND branch is merged
                    -> cleanup the worktree + merged branch

Read-only by default: the command prints exactly what it would do and exits
without touching the board or git repo. Passing ``--apply`` performs the
changes, one item at a time, refusing to drop any input it could not act on.

Logic lives in :mod:`flightdeck.core.hygiene` (pure, injectable). This module
only gathers inputs (registry, cards, git facts, worktree listing — all through
injectable handles) and presents the plan. Mirrors the ``topics`` command: the
connection is attached to ``args`` (``args.kdb`` / ``args.run``) so tests stub
it instead of touching a real board or repo.
"""

from __future__ import annotations

import argparse
import os
import sys

from ..core import git_state, hygiene, kanban, registry

# Boards are read across the whole host, not just those in the registry: an
# orphan board is itself a hygiene signal, and duplicates/triage make sense on
# any board. repo_by_board only supplies git facts for boards that are mapped.


def _git_facts_for_cards(
    cards: list[dict],
    projects: list[registry.Project],
    *,
    card_ids: set | None = None,
    _run=None,
) -> dict:
    """{card_id: {branch_exists, is_merged, commits_ahead}} via git_state.

    Only computes facts for ``card_ids`` when given (the minimal set the
    detectors actually need), otherwise for every card. A card's repo is
    resolved by its ``workspace_path`` attribution (:func:`kanban.project_for_card`),
    NOT by its board — the shared board holds cards from many projects, and a
    stale worktree card lives under the repo its workspace_path resolves to. A
    card that resolves to no project gets no entry, so hygiene defaults it to
    the conservative no-branch reading (never claims merged work we could not
    verify).
    """
    facts: dict[str, dict] = {}
    for card in cards:
        cid = card.get("id")
        if card_ids is not None and cid not in card_ids:
            continue
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
        facts[cid] = {
            "branch_exists": exists,
            "is_merged": merged,
            "commits_ahead": ahead,
        }
    return facts


def _collect_worktrees(
    projects: list[registry.Project],
    *,
    _listdir=None,
) -> list[dict]:
    """Every ``.worktrees/<card>`` checkout under each registered repo.

    Returns ``[{card_id, worktree, branch, board, repo}]``. The worktree path is
    resolved against each project's OWN repo path (repo-path attribution, not a
    board mapping), so a checkpoint is always attributed to the repo it actually
    lives under. ``branch`` is the convention ``wt/<card_id>``. Discovery is
    injectable (``_listdir``) so tests build the listing without touching the
    filesystem.
    """
    listdir = _listdir if _listdir is not None else os.listdir
    found: list[dict] = []
    seen_repos: set[str] = set()
    for proj in projects:
        repo = getattr(proj, "repo", None)
        if not repo or repo in seen_repos:
            continue  # one scan per unique repo path
        seen_repos.add(repo)
        root = os.path.join(repo, hygiene.WORKTREES_DIR)
        if not os.path.isdir(root):
            continue
        try:
            entries = sorted(listdir(root))
        except OSError:
            continue
        for entry in entries:
            if entry.startswith("."):
                continue
            found.append(
                {
                    "card_id": entry,
                    "worktree": os.path.join(root, entry),
                    "branch": f"{hygiene.BRANCH_PREFIX}{entry}",
                    "board": getattr(proj, "board", None),
                    "repo": repo,
                }
            )
    return found


# --------------------------------------------------------------------------- #
# presentation
# --------------------------------------------------------------------------- #


def _render(plan: dict) -> list[str]:
    lines: list[str] = []

    dup = plan["duplicates"]
    lines.append(f"DUPLICATES ({len(dup)})")
    if not dup:
        lines.append("  none")
    for group in dup:
        keeper = group["keep"]
        lines.append(
            f"  [{keeper['board']}] {keeper['title']!r} — keep {keeper['id']}, "
            f"archive {', '.join(c['id'] for c in group['archive'])}"
        )
    lines.append("")

    tri = plan["triage"]
    lines.append(f"TRIAGE TRAP ({len(tri)})")
    if not tri:
        lines.append("  none")
    for rescue in tri:
        card = rescue["card"]
        work = rescue["branch_has_work"]
        commits = rescue["commits_ahead"]
        summary = "work on branch" if work else "no work on branch"
        lines.append(
            f"  [{card['board']}] {card['title']!r} ({card['id']}) "
            f"branch={rescue['branch']} ({summary}, {commits} commits) — "
            f"archive + recreate preserving branch"
        )
    lines.append("")

    stale = plan["stale_worktrees"]
    lines.append(f"STALE WORKTREES ({len(stale)})")
    if not stale:
        lines.append("  none")
    for s in stale:
        lines.append(
            f"  [{s['board']}] {s['worktree']} (branch {s['branch']} merged)"
        )
    lines.append("")
    return lines


def _render_json(plan: dict) -> dict:
    return {
        "duplicates": [
            {
                "board": g["board"],
                "title": g["title"],
                "keep": g["keep"]["id"],
                "archive": [c["id"] for c in g["archive"]],
            }
            for g in plan["duplicates"]
        ],
        "triage": [
            {
                "board": r["card"]["board"],
                "card_id": r["card"]["id"],
                "title": r["card"]["title"],
                "branch": r["branch"],
                "branch_has_work": r["branch_has_work"],
                "commits_ahead": r["commits_ahead"],
            }
            for r in plan["triage"]
        ],
        "stale_worktrees": [
            {"card_id": s["card_id"], "board": s["board"], "worktree": s["worktree"]}
            for s in plan["stale_worktrees"]
        ],
    }


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #


def build_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "hygiene",
        help="detect board decay: duplicate cards, triage traps, stale worktrees",
        epilog="example: flightdeck hygiene --apply",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="perform the fixes (dry-run by default)",
    )
    p.add_argument(
        "--similarity",
        type=float,
        default=None,
        metavar="RATIO",
        help="title-similarity threshold for duplicate detection "
        f"(default: {hygiene.DEFAULT_SIMILARITY})",
    )
    p.set_defaults(func=cmd_hygiene)


def cmd_hygiene(args: argparse.Namespace, projects: list[registry.Project]) -> int:
    # Read ALL cards (including archived) in one pass. Archived cards are
    # essential for stale-worktree detection — an archived card is the prime
    # stale candidate and is excluded from the plain read — so we read them up
    # front rather than re-querying per detector. ``active`` is the non-archived
    # surface that duplicates / triage act on.
    all_cards = kanban.list_cards(board=None, include_archived=True)
    worktrees = _collect_worktrees(projects, _listdir=args.listdir)
    active = [c for c in all_cards if str(c.get("status") or "") != "archived"]
    closed_ids = {
        c["id"] for c in all_cards if str(c.get("status") or "") in hygiene.CLOSED_STATUSES
    }

    # Which cards need git facts? triage cards need the "CHECK git log
    # wt/<card>" work flag; worktree cards need is_merged. Computing facts for
    # that minimal union (not every card on the board) keeps the run cheap.
    worktree_ids = {w["card_id"] for w in worktrees}
    need_facts = {
        c["id"] for c in active if str(c.get("status") or "") == hygiene.TRIAGE_STATUS
    } | worktree_ids
    git_facts = _git_facts_for_cards(
        all_cards, projects, card_ids=need_facts, _run=args.run
    )

    threshold = args.similarity if args.similarity is not None else hygiene.DEFAULT_SIMILARITY
    plan = hygiene.build_plan(
        active, git_facts, worktrees, closed_ids, threshold=threshold
    )

    n_issues = len(plan["duplicates"]) + len(plan["triage"]) + len(plan["stale_worktrees"])

    if args.json:
        import json

        print(json.dumps(_render_json(plan)))
    else:
        if n_issues == 0:
            print("hygiene clean: no duplicate cards, no triage traps, no stale worktrees.")
            return 0
        for line in _render(plan):
            print(line)

    if args.apply:
        # Perform the fixes through the injectable handles attached by run().
        card_summary = hygiene.apply_card_plan(
            plan, _kdb=args.kdb
        )
        wt_summary = hygiene.apply_worktree_cleanup(
            plan["stale_worktrees"], _run=args.run
        )
        # Human notices go to stderr so --json keeps stdout pure JSON.
        print(
            f"applied: archived {len(card_summary['archived_duplicates'])} duplicate(s), "
            f"rescued {len(card_summary['recreated'])} triage card(s), "
            f"cleaned {len(wt_summary['removed'])} stale worktree(s).",
            file=sys.stderr,
        )
    else:
        print("dry-run: pass --apply to perform these fixes.", file=sys.stderr)
    return 0


def run(args: argparse.Namespace, registry_path: str) -> int:
    """Entry from cli.py: run hygiene with injectable handles attached.

    ``args.kdb`` / ``args.run`` / ``args.listdir`` default to None (use the
    real kanban library, real git, real filesystem); tests set them to fakes so
    nothing here touches a live system.
    """
    args.registry = registry_path
    args.kdb = getattr(args, "kdb", None)
    args.run = getattr(args, "run", None)
    args.listdir = getattr(args, "listdir", None)
    projects = registry.load_registry(registry_path)
    return cmd_hygiene(args, projects)
