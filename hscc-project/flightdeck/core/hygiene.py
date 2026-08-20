"""hygiene.py — detect and fix board decay.

Three decay modes, each observed on the live board before flightdeck existed:

1. DUPLICATES — near-identical card titles on one board. A crash-looping
   decomposer minted four "MCP Server Core" cards, two with identical titles.
   Detect by normalised-title similarity; propose archiving all but the newest.

2. TRIAGE TRAP — cards in the ``triage`` column are unrecoverable through the
   normal commands: ``unblock`` refuses ("not blocked/scheduled"), ``promote``
   refuses ("only todo or blocked"), and the only exits (``specify`` /
   ``decompose``) crash-loop on a NOT NULL ``tasks.session_id`` bug. Five cards
   were lost this way. Detect them and propose rescue: archive + recreate
   PRESERVING the branch reference — because the work is usually already done
   and must not be thrown away.

3. STALE WORKTREES — ``.worktrees/<card>`` directories and the branches they
   host, whose card is closed AND whose branch is merged. They accumulate
   indefinitely. Propose cleanup (never delete without --apply, never rm -rf).

This module holds the *decision logic* only. Every external call (git
subprocess, hermes kanban DB) is injectable — the tests stub them and never
touch a real repo, board, or network.

The pure plan builders (:func:`find_duplicates`, :func:`find_triage_traps`,
:func:`find_stale_worktrees`, :func:`build_plan`) mutate nothing. The
``apply_*`` helpers perform the mutations and operate through injectable
handles (``_kdb`` for the hermes kanban DB module, ``_run`` for git). All of
this is exercised only when the ``hygiene`` command is run with ``--apply``.
"""

from __future__ import annotations

import os
import re
from difflib import SequenceMatcher
from typing import Any, Callable, Optional

from . import git_state, kanban

# Statuses that mean "the card is settled and its branch may be cleaned up".
# ``done`` and ``archived`` are the normal closed states; ``cancelled`` is
# included defensively.
CLOSED_STATUSES = frozenset({"done", "archived", "cancelled"})

# The column that traps cards. Present in Hermes' VALID_STATUSES but with no
# working exit (see module docstring).
TRIAGE_STATUS = "triage"

# Where worktree-linked cards host their checkouts, relative to the repo root.
WORKTREES_DIR = ".worktrees"

# Normalised-title similarity above which two titles count as duplicates.
# Calibrated against the real board: genuine duplicates (the "MCP Server Core"
# pair at 1.0, RE-DO/FRESH variants at 0.91-0.95, the exact-repeat Stripe FIX-1
# cards at 1.0) all exceed 0.88, while the "Soconn C-XX" family of genuinely
# distinct platform cards tops out at 0.854. 0.88 separates them cleanly and is
# injectable so tests can exercise both sides of the line.
DEFAULT_SIMILARITY = 0.88

# The branch convention (mirrors kanban.BRANCH_PREFIX).
BRANCH_PREFIX = kanban.BRANCH_PREFIX


def normalize_title(title: Any) -> str:
    """Fold a title to a comparable form: lowercase, alphanumerics only,
    single spaces. Non-string input folds to the empty string."""
    if not isinstance(title, str):
        return ""
    text = re.sub(r"[^a-z0-9]+", " ", title.lower())
    return re.sub(r"\s+", " ", text).strip()


def _similar(a: str, b: str, threshold: float) -> bool:
    return SequenceMatcher(None, normalize_title(a), normalize_title(b)).ratio() >= threshold


# --------------------------------------------------------------------------- #
# Duplicates
# --------------------------------------------------------------------------- #


def find_duplicates(
    cards: list[dict],
    *,
    threshold: float = DEFAULT_SIMILARITY,
) -> list[dict]:
    """Group near-identical card titles *within one board*.

    Returns a list of groups; each group is::

        {
            "board": <board slug>,
            "title": <representative original title>,
            "keep": <card dict>,          # the newest — never archived
            "archive": [<card dict>, ...],# the rest — propose archiving
        }

    The keeper is the card with the greatest ``created_at`` (ties broken by
    lexicographic id so the choice is deterministic). Cards that are already
    archived are excluded — you cannot archive what is archived. A group of
    size 1 is not a duplicate and is never proposed.

    Never mutates ``cards``.
    """
    # Group by board first, so similarity never crosses board boundaries.
    by_board: dict[str, list[dict]] = {}
    for c in cards:
        if str(c.get("status") or "") == "archived":
            continue
        by_board.setdefault(str(c.get("board") or ""), []).append(c)

    groups: list[dict] = []
    for board, board_cards in by_board.items():
        # Greedy clustering. Sort by created_at ascending so the earliest card
        # becomes each group's representative; each subsequent card joins the
        # first existing group whose representative it resembles, else starts a
        # new group. Representative-only comparison keeps the genuine "C-01 /
        # C-02 / C-03" platform family from collapsing into one mega-group.
        ordered = sorted(
            board_cards,
            key=lambda c: (int(c.get("created_at") or 0), str(c.get("id") or "")),
        )
        clusters: list[list[dict]] = []
        for c in ordered:
            target = None
            for cluster in clusters:
                if _similar(cluster[0]["title"], c["title"], threshold):
                    target = cluster
                    break
            if target is None:
                clusters.append([c])
            else:
                target.append(c)

        for cluster in clusters:
            if len(cluster) < 2:
                continue
            keeper = max(cluster, key=lambda c: (int(c.get("created_at") or 0), str(c.get("id") or "")))
            archive = [c for c in cluster if c["id"] != keeper["id"]]
            groups.append(
                {
                    "board": board,
                    "title": keeper["title"],
                    "keep": keeper,
                    "archive": archive,
                }
            )
    return groups


# --------------------------------------------------------------------------- #
# Triage trap
# --------------------------------------------------------------------------- #


def _facts_for_git_facts(git_facts: dict, card_id: Optional[str]) -> dict:
    """Conservative git-fact defaults for one card (branch_exists=False,
    is_merged=False, commits_ahead=0) when no entry is present."""
    if card_id is None:
        return {"branch_exists": False, "is_merged": False, "commits_ahead": 0}
    facts = git_facts.get(card_id)
    if not isinstance(facts, dict):
        return {"branch_exists": False, "is_merged": False, "commits_ahead": 0}
    return {
        "branch_exists": bool(facts.get("branch_exists", False)),
        "is_merged": bool(facts.get("is_merged", False)),
        "commits_ahead": int(facts.get("commits_ahead", 0) or 0),
    }


def find_triage_traps(cards: list[dict], git_facts: dict) -> list[dict]:
    """Cards trapped in the ``triage`` column and the rescue for each.

    Rescue is archive + recreate PRESERVING the branch reference, because the
    work on that branch is usually already done and must not be thrown away.
    Each entry::

        {
            "card": <card dict>,          # the trapped card
            "branch": <str>,              # the branch its work lives on
            "branch_has_work": <bool>,    # git log shows commits ahead of main
            "commits_ahead": <int>,
            "recreate": {
                "title": <str>,
                "branch": <str>,          # recreated card keeps this branch
                "assignee": <str|None>,
            },
        }

    ``branch_has_work`` is computed from injected ``git_facts`` — this is the
    "CHECK ``git log wt/<card>`` FIRST" step: we never throw the branch away if
    it carries commits, and we surface that fact to the operator. Never mutates
    anything.
    """
    traps: list[dict] = []
    for c in cards:
        if str(c.get("status") or "") != TRIAGE_STATUS:
            continue
        cid = c.get("id")
        branch = c.get("branch") or f"{BRANCH_PREFIX}{cid}"
        facts = _facts_for_git_facts(git_facts, cid)
        ahead = facts["commits_ahead"]
        has_work = bool(facts["branch_exists"] and ahead > 0)
        traps.append(
            {
                "card": c,
                "branch": branch,
                "branch_has_work": has_work,
                "commits_ahead": ahead,
                "recreate": {
                    "title": c.get("title"),
                    "branch": branch,
                    "assignee": c.get("assignee"),
                },
            }
        )
    return traps


# --------------------------------------------------------------------------- #
# Stale worktrees / branches
# --------------------------------------------------------------------------- #


def find_stale_worktrees(
    worktrees: list[dict],
    git_facts: dict,
    closed_ids: set,
) -> list[dict]:
    """Worktree checkouts whose card is closed AND whose branch is merged.

    ``worktrees`` is a list of ``{"card_id", "board", "worktree", "branch"}`` —
    the existing ``.worktrees/<card>`` directories under each registered repo,
    as discovered by the caller. ``closed_ids`` is the set of card IDs that are
    closed (done/archived/cancelled); it is passed in explicitly because
    archived cards are excluded from the usual non-archived read, yet an
    archived card's worktree is precisely a prime stale candidate. ``git_facts``
    is ``{card_id: {branch_exists, is_merged, commits_ahead}}``.

    A worktree is stale only when BOTH hold:

    * its card's id is in ``closed_ids`` (done/archived/cancelled)
    * its branch is merged into main (per ``git_facts`` ``is_merged``)

    Returns a list of ``{"card_id", "board", "worktree", "branch"}``. Never
    mutates.
    """
    stale: list[dict] = []
    for wt in worktrees:
        cid = wt.get("card_id")
        if cid not in closed_ids:
            continue  # card still open — keep the worktree
        facts = _facts_for_git_facts(git_facts, cid)
        if not facts["is_merged"]:
            continue  # branch not merged — work may still be needed
        stale.append(
            {
                "card_id": cid,
                "board": wt.get("board"),
                "repo": wt.get("repo"),
                "worktree": wt["worktree"],
                "branch": wt["branch"],
            }
        )
    return stale


# --------------------------------------------------------------------------- #
# Composition
# --------------------------------------------------------------------------- #


def build_plan(
    cards: list[dict],
    git_facts: dict,
    worktrees: list[dict],
    closed_ids: set,
    *,
    threshold: float = DEFAULT_SIMILARITY,
) -> dict:
    """The full hygiene plan: ``{duplicates, triage, stale_worktrees}``.

    ``cards`` (non-archived read) drives duplicate + triage detection;
    ``closed_ids`` is the set of closed card ids (including archived) used by
    the stale-worktree detector. Pure — never mutates the board, git, or any
    input. The caller renders it and, only with an explicit ``--apply``, hands
    it to the ``apply_*`` helpers.
    """
    return {
        "duplicates": find_duplicates(cards, threshold=threshold),
        "triage": find_triage_traps(cards, git_facts),
        "stale_worktrees": find_stale_worktrees(worktrees, git_facts, closed_ids),
    }


# --------------------------------------------------------------------------- #
# Apply helpers — perform the mutations, all I/O through injectable handles.
# --------------------------------------------------------------------------- #


def _load_kdb():
    """The hermes kanban_db module for the apply path (mirrors kanban's loader).

    Apply is the only place any of this runs; the pure plan builders never need
    Hermes. Refuses with a clear error when Hermes is absent, reusing the
    existing loader so there is one code path to the library.
    """
    return kanban._load_kanban_db()


def apply_card_plan(
    plan: dict,
    *,
    _kdb: Any = None,
) -> dict:
    """Apply the board-mutating parts of a plan: duplicate archiving and triage
    rescue (archive + recreate preserving the branch).

    ``_kdb`` is the hermes kanban_db module (or any stand-in exposing
    ``connect``, ``archive_task``, ``create_task``, ``get_task``). When None,
    the real library is loaded. Each operation is idempotent at the item level:
    ``archive_task`` returns False for an already-archived card, so re-running
    never double-writes.

    Returns a summary dict::

        {
            "archived_duplicates": [ids],
            "archived_triage": [ids],
            "recreated": [{old_id, new_id}],
        }
    """
    kdb = _kdb if _kdb is not None else _load_kdb()
    summary: dict = {"archived_duplicates": [], "archived_triage": [], "recreated": []}

    # Duplicates: archive all but the keeper.
    for group in plan.get("duplicates", []):
        keep_id = group["keep"]["id"]
        conn = kdb.connect(board=group["board"])
        try:
            for c in group["archive"]:
                if c["id"] == keep_id:
                    continue
                if kdb.archive_task(conn, c["id"]):
                    summary["archived_duplicates"].append(c["id"])
        finally:
            conn.close()

    # Triage rescue: archive the trapped card, recreate preserving the branch.
    for rescue in plan.get("triage", []):
        card = rescue["card"]
        rc = rescue["recreate"]
        conn = kdb.connect(board=card["board"])
        try:
            old_id = card["id"]
            if kdb.archive_task(conn, old_id):
                summary["archived_triage"].append(old_id)
            # Fetch the body from the DB at recreate time — the reproduced card
            # must carry the full spec, not just the title. Fall back to the
            # plan's title when the body cannot be read.
            body = None
            try:
                task = kdb.get_task(conn, old_id)
                body = getattr(task, "body", None)
            except Exception:  # defensive — body is best-effort
                body = None
            new_id = kdb.create_task(
                conn,
                title=rc["title"],
                body=body,
                assignee=rc["assignee"],
                branch_name=rc["branch"],
                workspace_kind="worktree",
                board=card["board"],
            )
            # No ``initial_status`` on purpose: create_task's only initial
            # statuses are 'blocked'/'running', and its default (running)
            # computes to ``ready`` for a parentless card — exactly the
            # actionable queue state we want, avoiding both 'blocked' and
            # re-trapping it in 'triage'.
            summary["recreated"].append({"old_id": old_id, "new_id": new_id})
        finally:
            conn.close()
    return summary


def _default_run(cmd, repo):
    return git_state._default_run(cmd, repo)


def completion_guard(card: dict, *, _run: Optional[Callable] = None) -> Optional[str]:
    """Refuse to complete/block a card whose worktree has uncommitted changes.

    The preventive fix for the recurring lost-work incident: flightdeck
    workers repeatedly marked cards ``done`` (or blocked them for review)
    without ever running ``git commit``/``git push``, leaving their
    implementation stranded as uncommitted changes in a worktree that the
    hygiene pass later reaps — nothing ever merged. This guard makes that
    impossible to do silently.

    Given a card dict (as produced by :func:`kanban.list_cards` / ``find_card``,
    carrying ``workspace_path`` for worktree-kind cards), it returns:

    * ``None`` when the card may proceed — its worktree is clean (every change
      committed) or the card has no worktree to check (scratch cards).
    * a human-readable REFUSAL message when the worktree has modified/staged/
      untracked files, naming the offending paths, so the worker knows exactly
      what to commit before it can complete or block for review.

    The check is advisory-safe: it only refuses when uncommitted changes
    actually exist; a clean-and-committed worktree passes untouched. git facts
    flow through :func:`git_state.uncommitted_files` on the same injectable
    ``_run`` seam the ``apply_*`` helpers use, so tests stub it without a real
    repository. Never mutates anything.
    """
    worktree = card.get("workspace_path")
    if not worktree:
        return None  # no worktree to check (scratch card) — nothing to lose
    dirty = git_state.uncommitted_files(worktree, _run=_run)
    if not dirty:
        return None  # clean worktree — completion/block may proceed
    paths = ", ".join(dirty)
    name = card.get("id") or card.get("title") or "card"
    return (
        f"cannot complete {name}: uncommitted changes in {paths} "
        f"— commit them first (the worktree must be clean before marking "
        f"done or blocked-for-review)"
    )


def apply_worktree_cleanup(
    stale: list[dict],
    *,
    _run: Optional[Callable] = None,
) -> dict:
    """Remove stale worktrees and their merged branches.

    For each stale entry, act on the ``repo`` it carries (resolved by the
    collector via repo-path attribution, NOT a board slug -> repo mapping — a
    stale worktree lives under the repo its workspace_path resolves to) and run,
    in order::

        git worktree remove <path>
        git branch -d <branch>

    ``git worktree remove`` refuses to delete a dirty worktree (unless forced,
    which we deliberately never do), and ``git branch -d`` is the *safe* delete
    that fails when the branch is not merged. Neither is ``rm -rf``. We only
    ever propose worktrees whose branch is already merged, so ``-d`` succeeds.

    A stale entry with no resolvable repo is recorded as ``failed`` (never
    acted on) — we cannot verify its repo, so we do not touch it.

    Returns ``{"removed": [{"card_id", "worktree"}], "failed": [...]}``.
    """
    runner = _run if _run is not None else _default_run
    removed: list[dict] = []
    failed: list[dict] = []
    for entry in stale:
        repo = entry.get("repo")
        if not repo:
            failed.append({"card_id": entry["card_id"], "reason": "no repo resolved for worktree"})
            continue
        wt_removed = runner(["git", "worktree", "remove", entry["worktree"]], repo)
        if wt_removed.returncode != 0:
            failed.append(
                {
                    "card_id": entry["card_id"],
                    "worktree": entry["worktree"],
                    "reason": wt_removed.stderr.strip() or wt_removed.stdout.strip(),
                }
            )
            continue
        # Branch delete is best-effort secondary; a missing branch (already
        # deleted) is fine, but we never force-delete an unmerged one.
        runner(["git", "branch", "-d", entry["branch"]], repo)
        removed.append({"card_id": entry["card_id"], "worktree": entry["worktree"]})
    return {"removed": removed, "failed": failed}
