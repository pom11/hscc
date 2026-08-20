"""why.py — `flightdeck why <card_id>` : one card's full story.

Assembling a card's story by hand across the board and git is a recurring tax
during review. ``why`` reads ONE card and renders, in a fixed order:

  identity  — title, status, assignee, board, project (attributed by repo
              path via :func:`kanban.project_for_card`), and the milestone
              from a ``MILESTONE: <id>`` body tag if present.
  timing    — created, started, last heartbeat, and how long in the current
              status (most recent status-transition event, else started_at).
  branch    — whether it exists, its commits (subjects), whether it LANDED
              (via :func:`git_state.landed_via_merge` — being an ancestor of
              main is not proof), and uncommitted files in its worktree.
  workspace — the path, and whether it is a worktree or the repo root.
  verdict   — ONE line saying what state this card is actually in and what
              would move it.

The verdict is the point. It distinguishes, from facts already available:

  STARVED          empty worktree (infrastructure) — what would move it: the
                   executor has to provision files.
  WORKING          files present but no commit.
  AWAITING REVIEW  commits but unmerged (needs eyes).
  LANDED           merged (the card should be closed).
  NO BRANCH        no branch at all (unstarted / unattributed).

These distinctions cost hours of real debugging to learn; this command encodes
them. An unknown card id is an ERROR naming the boards searched, never an
empty result.

All external access is injectable: ``_run`` (git subprocess), ``_now`` (clock),
``_kdb`` (hermes kanban library), ``_events`` (per-card event reader) so tests
never touch a real board, repo, or the network.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from typing import Callable, Optional

from ..core import git_state, kanban, registry

# A ``MILESTONE: <id>`` line links a card to a roadmap milestone (see start.py
# and decompose.py — the tag is stamped into card bodies). Matches whole-line
# so a mention inside a paragraph does not count.
_MILESTONE_RE = re.compile(
    r"^\s*MILESTONE\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE
)


class UnknownCardError(Exception):
    """Raised when a card id matches no card on any board searched."""


def _milestone_id(body: Optional[str]) -> Optional[str]:
    """The milestone a card links to via a ``MILESTONE: <id>`` body line.

    Returns the milestone id (e.g. ``review-loop``) or None when the body has
    no such tag. Mirrors the established parser in ``commands/start.py``.
    """
    if not body:
        return None
    m = _MILESTONE_RE.search(body)
    return m.group(1).strip() if m else None


def _is_worktree(project_repo: str, workspace_path: Optional[str]) -> bool:
    """True when the card's workspace is a worktree under the project repo.

    A card whose resolved workspace_path equals the repo ROOT runs directly in
    the main (operator) checkout — not a worktree. Anything under ``<repo>/``
    that is not the root (the ``.worktrees/<card>`` convention) is a worktree.
    Reuses kanban's own path resolution so this is never a second
    implementation.
    """
    workspace = kanban._resolve_path(workspace_path)
    repo = kanban._resolve_path(project_repo)
    if not workspace or not repo:
        return False
    if workspace == repo:
        return False
    return workspace.startswith(repo + os.sep)


def _status_dir(project_repo: str, workspace_path: Optional[str]) -> str:
    """The working tree to check for uncommitted files.

    A card running in a worktree has its files there, so we check the worktree
    checkout. A card running in (or pointed at) the repo root checks the repo.
    Falls back to the repo when the workspace path is missing — the safest
    single directory to inspect.
    """
    workspace = kanban._resolve_path(workspace_path)
    repo = kanban._resolve_path(project_repo)
    if workspace and workspace != repo and workspace.startswith(repo + os.sep):
        return workspace
    return repo


def gather(
    card_id: str,
    projects: list[registry.Project],
    *,
    _run: Optional[Callable] = None,
    _now: Optional[Callable] = None,
    _kdb=None,
    _events: Optional[Callable] = None,
    _boards: Optional[list] = None,
) -> dict:
    """Assemble the full story for one card, or raise UnknownCardError.

    Reads exactly one card via :func:`kanban.find_card` (which searches every
    board, or ``_boards``), attributes it to a project by repo path, and
    gathers its git facts through the injectable ``_run``. Returns a ``story``
    dict carrying identity, timing, branch, workspace and verdict — the single
    structure the renderers consume.

    Raises :class:`UnknownCardError` (naming the boards searched) when the id
    matches no card. ``_events(card)`` returns the card's event list (defaults
    to an empty list) so ``how long in this status`` can be computed from
    status-transition events.
    """
    now = int(float(_now())) if _now is not None else int(time.time())
    events = _events(card_id) if _events is not None else []

    boards_searched = kanban.list_boards_searched(_kdb=_kdb, boards=_boards)
    card = kanban.find_card(card_id, _kdb=_kdb, boards=_boards)
    if card is None:
        searched = ", ".join(boards_searched) or "(none)"
        raise UnknownCardError(
            f"unknown card id {card_id!r}: no card found on board(s) {searched}"
        )

    project = kanban.project_for_card(card, projects)
    project_name = (
        getattr(project, "name", None) if project is not kanban.UNATTRIBUTED else None
    )
    repo = getattr(project, "repo", None) if project is not kanban.UNATTRIBUTED else None

    branch = card.get("branch")
    branch_exists = False
    commits: list[str] = []
    landed = False
    uncommitted: list[str] = []
    if repo and branch:
        branch_exists = git_state.branch_exists(repo, branch, _run=_run)
        if branch_exists:
            commits = git_state.commit_subjects(repo, branch, _run=_run)
            landed = git_state.landed_via_merge(repo, branch, _run=_run)
            # "Uncommitted files in its WORKTREE": for a worktree card the
            # working-tree files live in the worktree checkout (which shares git
            # state with the repo but has its own checkout), so run the status
            # check THERE — querying the repo root would miss the worker's files
            # and falsely read STARVED. For a repo-root card, the repo is the
            # working tree.
            status_dir = _status_dir(repo, card.get("workspace_path"))
            uncommitted = git_state.uncommitted_files(status_dir, _run=_run)

    status_duration = kanban.status_duration_seconds(card, events, now=now)

    story = {
        "id": card_id,
        "title": card.get("title") or "(untitled)",
        "status": str(card.get("status") or ""),
        "assignee": card.get("assignee"),
        "board": str(card.get("board") or ""),
        "project": project_name,
        "milestone": _milestone_id(card.get("body")),
        "created_at": _int_or_none(card.get("created_at")),
        "started_at": _int_or_none(card.get("started_at")),
        "last_heartbeat_at": _int_or_none(card.get("last_heartbeat_at")),
        "status_duration_s": status_duration,
        "branch": branch,
        "branch_exists": branch_exists,
        "commits": commits,
        "landed": landed,
        "uncommitted": uncommitted,
        "workspace_path": card.get("workspace_path"),
        "is_worktree": _is_worktree(repo, card.get("workspace_path")) if repo else False,
        "boards_searched": boards_searched,
    }
    story["verdict"] = verdict(story)
    return story


def _int_or_none(value) -> Optional[int]:
    """Coerce ``value`` to int, or None when absent/unparseable."""
    if not value:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def verdict(story: dict) -> str:
    """ONE line: what state this card is in and what would move it.

    Distinguishes the five states, using only facts already in ``story``:

      LANDED          — branch exists and merged via ``landed_via_merge``
                        (the card should be closed).
      STARVED         — branch exists, no commits, empty worktree
                        (infrastructure: the executor handed the worker no
                        files).
      WORKING         — branch exists, no commits, files present in the
                        worktree (worker mid-work, has not committed yet).
      AWAITING REVIEW — branch exists with commits, but unmerged (needs eyes).
      NO BRANCH       — branch absent (unstarted, or cannot be attributed to a
                        repo to check).

    Precedence: landed wins over everything (merged work is settled); among
    the unmerged branch states we distinguish by commits/worktree; no-branch
    is the residual. The message always names the state and what would move it.
    """
    title = story.get("title") or story.get("id")
    branch_exists = bool(story.get("branch_exists"))
    landed = bool(story.get("landed"))
    commits = story.get("commits") or []
    uncommitted = story.get("uncommitted") or []

    if branch_exists and landed:
        return f"LANDED: {title!r} — branch merged into main; close the card."
    if not branch_exists:
        return (
            f"NO BRANCH: {title!r} — no branch {story.get('branch')!r}; "
            "unstarted or unattributable to a repo."
        )
    if commits:
        return (
            f"AWAITING REVIEW: {title!r} — {len(commits)} commit(s) unmerged; "
            "get it reviewed and merged."
        )
    if uncommitted:
        return (
            f"WORKING: {title!r} — files present, no commit yet; commit the work."
        )
    return (
        f"STARVED: {title!r} — worktree empty, no commits; the executor provisioned "
        "no files."
    )


def _fmt_ts(ts: Optional[int]) -> str:
    """A unix timestamp as a local ``YYYY-MM-DD HH:MM`` string, or ``-``."""
    if ts is None:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _fmt_duration(seconds: Optional[int]) -> str:
    """A duration in seconds as compact human text, or ``-`` when unknown."""
    if seconds is None:
        return "-"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {seconds % 3600 // 60}m"
    return f"{seconds // 86400}d {seconds % 86400 // 3600}h"


def _indent(lines: list[str], prefix: str = "    ") -> list[str]:
    return [prefix + ln if ln else ln for ln in lines]


def render(story: dict) -> list[str]:
    """Render one card's story as human lines, one per section."""
    out: list[str] = []
    out.append(f"[{story.get('id')}] {story.get('title')}")
    out.append("")

    out.append("IDENTITY")
    out.append(f"  status    {story.get('status') or '-'}")
    out.append(f"  assignee  {story.get('assignee') or '-'}")
    out.append(f"  board     {story.get('board') or '-'}")
    out.append(f"  project   {story.get('project') or 'UNATTRIBUTED'}")
    milestone = story.get("milestone")
    out.append(f"  milestone {milestone if milestone is not None else '-'}")
    out.append("")

    out.append("TIMING")
    out.append(f"  created         {_fmt_ts(story.get('created_at'))}")
    out.append(f"  started         {_fmt_ts(story.get('started_at'))}")
    out.append(f"  last heartbeat  {_fmt_ts(story.get('last_heartbeat_at'))}")
    out.append(
        f"  in current status {_fmt_duration(story.get('status_duration_s'))}"
    )
    out.append("")

    out.append("BRANCH")
    branch = story.get("branch")
    if not story.get("branch_exists"):
        out.append(f"  {branch or '-'}  (does not exist)")
        if story.get("commits"):
            out.extend(_indent(["commits:"], "  "))
            out.extend(_indent(story["commits"], "    "))
        out.append(f"  landed  {str(story.get('landed')).lower()}")
    else:
        out.append(f"  {branch}  (exists)")
        commits = story.get("commits") or []
        out.append(f"  commits ({len(commits)})")
        out.extend(_indent(commits, "    "))
        out.append(f"  landed  {str(story.get('landed')).lower()}")
        uncommitted = story.get("uncommitted") or []
        out.append(f"  uncommitted ({len(uncommitted)})")
        out.extend(_indent(uncommitted, "    "))
    out.append("")

    out.append("WORKSPACE")
    out.append(f"  path       {story.get('workspace_path') or '-'}")
    kind = "worktree" if story.get("is_worktree") else "repo root"
    out.append(f"  kind       {kind}")
    out.append("")

    out.append("VERDICT")
    out.append(f"  {story.get('verdict')}")
    return out


def render_json(story: dict) -> dict:
    """The same story as a structured dict (for ``--json``)."""
    return {
        "id": story.get("id"),
        "title": story.get("title"),
        "status": story.get("status"),
        "assignee": story.get("assignee"),
        "board": story.get("board"),
        "project": story.get("project"),
        "milestone": story.get("milestone"),
        "created_at": story.get("created_at"),
        "started_at": story.get("started_at"),
        "last_heartbeat_at": story.get("last_heartbeat_at"),
        "status_duration_s": story.get("status_duration_s"),
        "branch": story.get("branch"),
        "branch_exists": story.get("branch_exists"),
        "commits": story.get("commits"),
        "landed": story.get("landed"),
        "uncommitted": story.get("uncommitted"),
        "workspace_path": story.get("workspace_path"),
        "is_worktree": story.get("is_worktree"),
        "verdict": story.get("verdict"),
        "boards_searched": story.get("boards_searched"),
    }


def build_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "why",
        help="one card's full story across kanban and git",
        epilog="example: flightdeck why t_942dcbd1",
    )
    p.add_argument("card_id", metavar="CARD_ID", help="card id to inspect")
    p.set_defaults(func=cmd_why)


def cmd_why(args: argparse.Namespace, projects: list[registry.Project]) -> int:
    try:
        story = gather(
            args.card_id,
            projects,
            _run=args.run,
            _now=args.now,
            _kdb=args.kdb,
            _events=args.events,
            _boards=args.boards,
        )
    except UnknownCardError as exc:
        print(f"flightdeck: {exc}", file=args.stderr or sys.stderr)
        return 1
    if args.json:
        print(json.dumps(render_json(story)))
    else:
        for line in render(story):
            print(line)
    return 0


def run(args: argparse.Namespace, registry_path: str) -> int:
    """Entry from cli.py: resolve injectable handles, then render."""
    args.registry = registry_path
    args.run = getattr(args, "run", None)
    args.now = getattr(args, "now", None)
    args.kdb = getattr(args, "kdb", None)
    args.events = getattr(args, "events", None)
    args.boards = getattr(args, "boards", None)
    args.stderr = getattr(args, "stderr", None) or sys.stderr
    args.json = getattr(args, "json", False)
    projects = registry.load_registry(registry_path)
    return cmd_why(args, projects)
