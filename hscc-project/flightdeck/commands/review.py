"""review.py — `flightdeck review` — card review + awaiting-review queue + gate.

Three concerns, one command, presentation only; all logic is in
:mod:`flightdeck.core.review` composed with the existing core modules
(:mod:`~flightdeck.core.kanban` for cards, :mod:`~flightdeck.core.git_state`
for branch-merge facts, and :mod:`~flightdeck.core.registry`):

1. ``flightdeck review <card-id>`` — resolve a card, show its diff, merge+close
   (P0 in FEATURES.md). Resolve card -> branch (``wt/<card_id>`` convention)
   -> project -> repo, refuse early if the branch is already an ancestor of
   ``main`` ("already landed, nothing to review"), show commit subject / files
   changed / insertions+deletions / whether the branch merges cleanly
   (``git merge-tree`` conflict-marker count), show the card's ``VERIFY:``
   line (and say loudly if it is ABSENT). Default is DRY-RUN; ``--apply``
   merges the branch into ``main`` AND closes the card as ONE action.

2. ``flightdeck review --queue`` — everything GENUINELY awaiting review across
   all registered projects: card is review-required/blocked AND its branch is
   NOT an ancestor of ``main``. Newest first, with project, card id, branch
   and age. The same strict rule behind standup's NEEDS YOU, so a merged
   branch is never listed.

3. ``flightdeck review <project>`` — run that project's ``verify`` command and
   apply the TEST-QUALITY GATE before merge: flag any single test over 1s, a
   total suite slower than the project's recorded baseline, or evidence of
   network access in the tests. The gate blocks a merge even on a green-but-
   slow suite (a shipped change made a suite 33x slower with all tests green,
   caught only by reading ``--durations`` by hand).

Flow (all I/O injectable so tests never touch git, the network, Telegram, or
a live board):

1. resolve card -> branch (``wt/<card_id>`` convention from core/kanban)
   -> project -> repo (core/registry, by the SAME workspace_path attribution
   rule as reconcile — ``kanban.project_for_card``)
2. refuse early if the branch is already an ancestor of ``main``
   ("already landed, nothing to review")
3. show: commit subject, files changed, insertions/deletions, whether the
   branch merges cleanly (``git merge-tree`` conflict-marker count)
4. show the card's ``VERIFY:`` line; if ABSENT, say so loudly — without it
   the operator must reverse-engineer what to test, which is the toil this
   attacks
5. default is DRY-RUN (show everything, change nothing); ``--apply`` merges
   the branch into ``main`` AND closes the card as ONE action

Every external call is injectable: ``_run`` (a ``(cmd_list, repo) ->
process`` runner, same contract as core/git_state) and ``_close_card`` (a
``(card_id, board) -> bool`` callable that archives the card on the board;
returns True only when the archive succeeded). ``run()`` wires the real
implementation when none is injected. Nothing here imports Hermes directly;
core/kanban does that, and it is stubbed in tests.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time

from ..core import git_state, kanban, registry, review
from ..core.review import BaselineStore

# The base branch every card's work is reviewed against and merged into.
DEFAULT_BASE = "main"

# Conflict markers: git merge-tree --write-tree emits one `<<<<<<<` line per
# conflicted file region on a conflicting merge. Zero markers == clean merge.
_CONFLICT_MARKER = "<<<<<<<"

# A `VERIFY:` line, case-insensitive, optionally bulleted/indented, capturing
# everything after the colon as the verification command/instruction.
_VERIFY_RE = re.compile(
    r"^\s*(?:-\s+)?verify\s*:\s*(.*)$", re.MULTILINE | re.IGNORECASE
)


class ReviewError(Exception):
    """Base for review errors — resolving, incomplete, or already landed."""


class _Proc:
    """Minimal process-shaped object for the injectable runner fallback."""

    def __init__(self, cmd, returncode, stdout="", stderr=""):
        self.cmd = cmd
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _default_run(cmd, repo):
    """Production runner: run a git command in ``repo`` (subprocess argv form).

    Returns a process-like object with ``.returncode``/``.stdout``/``.stderr``
    (all str). A missing repo or git raises -> synthetic failed process
    (returncode 128) so callers degrade gracefully instead of tracing.
    """
    try:
        return subprocess.run(
            cmd,
            cwd=repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        return _Proc(cmd, 128, "", str(exc))


def _dispatch(cmd, repo, runner):
    if runner is not None:
        return runner(cmd, repo)
    return _default_run(cmd, repo)


# --------------------------------------------------------------------------- #
# Pure helpers (no I/O — unit tested directly)
# --------------------------------------------------------------------------- #


def _verify_line(body: str | None) -> tuple[bool, str]:
    """The card's ``VERIFY:`` line. Returns ``(present, text)``.

    ``present`` is False only when no VERIFY marker exists. When the marker is
    present, ``text`` is everything after the colon (possibly empty, meaning
    the card names a VERIFY step but provides no command — itself worth
    surfacing, not silently passing).
    """
    if not body:
        return (False, "")
    m = _VERIFY_RE.search(body)
    if not m:
        return (False, "")
    return (True, m.group(1).strip())


def _parse_numstat(stdout: str) -> tuple[int, int, int]:
    """Parse ``git diff --numstat`` into ``(files, insertions, deletions)``.

    Each non-blank line is ``<add>\t<del>\t<path>``. Binary files report ``-``
    for both counts and count as one file changed with zero l/t; empty input
    (a clean or non-diff) yields all zeros.
    """
    files = 0
    insertions = 0
    deletions = 0
    for line in stdout.splitlines():
        if not line.strip():
            continue
        files += 1
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        for idx, attr in ((0, "ins"), (1, "del")):
            try:
                n = int(parts[idx])
            except ValueError:
                continue  # binary files report '-'
            if attr == "ins":
                insertions += n
            else:
                deletions += n
    return files, insertions, deletions


def _conflict_count(stdout: str) -> int:
    """Number of ``<<<<<<<`` conflict markers in a merge-tree's output."""
    return stdout.count(_CONFLICT_MARKER)


def _find_card(cards: list[dict], card_id: str) -> dict | None:
    """The card whose id matches ``card_id`` (string-compared), or None."""
    needle = str(card_id)
    for c in cards:
        if str(c.get("id")) == needle:
            return c
    return None


def _resolve(cards, projects, card_id):
    """Resolve card -> (card, project, branch).

    Raises ReviewError when the card cannot be found or no registry project's
    repo owns the card's workspace_path (so the repo is unresolvable rather
    than guessed).

    Project attribution uses the SAME rule as ``reconcile`` —
    :func:`kanban.project_for_card`, which matches the card's ``workspace_path``
    against each project's ``repo`` — so the two commands can never disagree
    about which project's git a card belongs to. (The card's ``board`` slug is
    used only to locate the card's DB for close/archive, not to pick the repo.)
    """
    card = _find_card(cards, card_id)
    if card is None:
        raise ReviewError(
            f"no card with id {card_id!r} found on any board; check the id"
        )
    branch = card.get("branch")
    if not branch:
        raise ReviewError(f"card {card_id!r} has no resolvable branch")
    project = kanban.project_for_card(card, projects)
    if project is kanban.UNATTRIBUTED:
        raise ReviewError(
            f"card {card_id!r}'s workspace_path {card.get('workspace_path')!r} "
            "does not resolve to any registry project's repo; cannot resolve "
            "its git repo"
        )
    return card, project, branch


# --------------------------------------------------------------------------- #
# Diff / merge facts
# --------------------------------------------------------------------------- #


def _branch_facts(repo, branch, base=DEFAULT_BASE, _run=None):
    """Commit subject + diff stats + merge cleanliness for ``branch``.

    Returns a dict ``{subject, files, insertions, deletions, conflicts,
    exists}``. ``exists`` is False when the branch does not resolve in ``repo``
    (we report that rather than fabricate a diff). Every field degrades to a
    safe value so presentation never crashes on a bad repo.
    """
    if not git_state.branch_exists(repo, branch, _run=_run):
        return {
            "exists": False,
            "subject": None,
            "files": 0,
            "insertions": 0,
            "deletions": 0,
            "conflicts": None,
        }

    subject = None
    log = _dispatch(["git", "log", "-1", "--format=%s", branch], repo, _run)
    if log.returncode == 0:
        subject = (log.stdout or "").strip() or None

    files = insertions = deletions = 0
    numstat = _dispatch(
        ["git", "diff", "--numstat", f"{base}...{branch}"], repo, _run
    )
    if numstat.returncode == 0:
        files, insertions, deletions = _parse_numstat(numstat.stdout)

    conflicts = None
    mtree = _dispatch(
        ["git", "merge-tree", "--write-tree", base, branch], repo, _run
    )
    if mtree.returncode in (0, 1):
        # Both a clean (0) and a conflicting (1) merge exit within our handling;
        # only a hard failure (e.g. unknown base/branch, pre-2.38 git) is None.
        conflicts = _conflict_count(mtree.stdout)

    return {
        "exists": True,
        "subject": subject,
        "files": files,
        "insertions": insertions,
        "deletions": deletions,
        "conflicts": conflicts,
    }


# --------------------------------------------------------------------------- #
# The merge + close performed by --apply
# --------------------------------------------------------------------------- #


def _do_apply(repo, branch, base=DEFAULT_BASE, _run=None, status_out=None):
    """Merge ``branch`` into ``base`` and push ``base``.

    Returns the human-readable outcome string. The command is
    ``main <- branch``: checkout base, merge the branch in, then push base.
    A failure on any step returns a message naming the step, so the operator
    knows exactly where it stopped; nothing is silently skipped.
    """
    if _dispatch(["git", "checkout", base], repo, _run).returncode != 0:
        return f"merge failed: could not checkout {base}"
    if _dispatch(["git", "merge", branch], repo, _run).returncode != 0:
        return f"merge failed: conflict merging {branch} into {base}"
    if _dispatch(["git", "push", "origin", base], repo, _run).returncode != 0:
        return "merge done but push failed: run `git push origin %s` by hand" % base
    return f"merged {branch} into {base} and pushed"


def _real_close_card(card_id: str, board) -> bool:
    """Archive a card on its board via Hermes' kanban library.

    The PRODUCTION close-card implementation — the seam that closes the gap
    this module historically had (the real CLI never wired a real close, so
    ``review --apply`` claimed card closed without archiving). Reuses Hermes'
    own ``archive_task`` (marks status ``archived`` — the same path ``reconcile``
    and ``migrate-card`` already use) so the card stops showing up in
    ``standup``'s RUNNING bucket and ``review --queue``.

    Returns ``True`` only when the archive genuinely succeeded. ``archive_task``
    returns ``False`` when the row did not match (e.g. the card is already
    archived). We surface that rather than claiming success unconditionally.
    """
    if not board:
        return False
    kdb = kanban._load_kanban_db()
    conn = kdb.connect(board=board)
    try:
        return bool(kdb.archive_task(conn, card_id))
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Presentation
# --------------------------------------------------------------------------- #


def _render(card, project, branch, facts, verify_present, verify_text,
            projects=None, *, apply_outcome=None):
    lines: list[str] = []
    lines.append(
        f"card: {card.get('id')}  {card.get('title') or '(untitled)'}"
    )
    lines.append(
        f"      board {card.get('board') or '?'}  project {project.name}  "
        f"repo {project.repo}"
    )
    lines.append(f"branch: {branch}  (base: {DEFAULT_BASE})")
    if projects is not None:
        notice = registry.dependent_notice(project.name, projects)
        if notice:
            lines.append(f"dependents: {notice}")

    if not facts["exists"]:
        lines.append("")
        lines.append(
            f"error: branch {branch!r} does not exist in {project.repo}; "
            "nothing to review or merge."
        )
        return lines

    lines.append("")
    lines.append(f"[commit] {facts['subject'] or '(unknown subject)'}")
    lines.append(
        f"        {facts['files']} file(s) changed, "
        f"{facts['insertions']} insertion(s)(+), "
        f"{facts['deletions']} deletion(s)(-)"
    )
    clean = facts["conflicts"] == 0
    if facts["conflicts"] is None:
        lines.append(f"MERGE: unknown (could not run merge-tree for {branch})")
    elif clean:
        lines.append(f"MERGE: clean — merges into {DEFAULT_BASE} without conflict")
    else:
        lines.append(
            f"MERGE: {facts['conflicts']} conflict(s) — will need resolution"
        )

    lines.append("")
    if verify_present:
        if verify_text:
            lines.append(f"VERIFY: {verify_text}")
        else:
            lines.append("VERIFY: <present but empty — names a check but no command>")
    else:
        lines.append(
            "!!! NO VERIFY LINE — this card does not say how to check the work. "
            "Reverse-engineer what to test before merging."
        )

    if apply_outcome is not None:
        lines.append("")
        lines.append(apply_outcome)

    return lines


def _render_json(card, project, branch, facts, verify_present, verify_text,
                 projects=None, landing=None, apply_outcome=None):
    return {
        "id": card.get("id"),
        "title": card.get("title"),
        "board": card.get("board"),
        "project": project.name,
        "repo": project.repo,
        "branch": branch,
        "base": DEFAULT_BASE,
        "subject": facts["subject"],
        "files_changed": facts["files"],
        "insertions": facts["insertions"],
        "deletions": facts["deletions"],
        "conflicts": facts["conflicts"],
        "landed": landing,
        "verify_present": verify_present,
        "verify": verify_text,
        "apply_outcome": apply_outcome,
        "dependents": (
            registry.dependent_notice(project.name, projects)
            if projects is not None else None
        ),
    }


# --------------------------------------------------------------------------- #
# command entry
# --------------------------------------------------------------------------- #


def cmd_review(args: argparse.Namespace) -> int:
    """Resolve, present, and (on --apply) merge + close. Returns exit code.

    Injects from ``args`` (set by :func:`run` / :func:`review`): ``_run``,
    ``_close_card``, ``_now``, ``json``. All defaults pass through to the real
    external systems when not provided.
    """
    _run = getattr(args, "run", None)
    _close_card = getattr(args, "close_card", None)
    json_out = bool(getattr(args, "json", False))
    base = getattr(args, "base", DEFAULT_BASE) or DEFAULT_BASE
    card_id = args.card
    projects = registry.load_registry(args.registry)
    # Read cards through core/kanban (injectable in run()).
    cards = getattr(args, "cards", None)
    if cards is None:
        cards = kanban.list_cards()

    try:
        card, project, branch = _resolve(cards, projects, card_id)
    except ReviewError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    repo = project.repo

    # REFUSE if already landed: branch is already an ancestor of main. There is
    # nothing left to review — reporting a diff would mislead the operator into
    # re-reviewing work that shipped. (is_merged degrades to False for a
    # branch we cannot resolve; that case is handled as missing below.)
    landed = git_state.is_merged(repo, branch, base, _run=_run)
    if landed:
        message = f"already landed, nothing to review ({branch} is already an ancestor of {base})"
        if json_out:
            import json

            print(json.dumps(
                {"id": card_id, "refuse": message} | _render_json(
                    card, project, branch,
                    _branch_facts(repo, branch, base, _run),
                    *_verify_line(card.get("body")),
                    projects=projects,
                    landing=True,
                )
            ))
            return 1
        print(message)
        return 1

    facts = _branch_facts(repo, branch, base, _run)
    verify_present, verify_text = _verify_line(card.get("body"))

    if not args.apply:
        # DRY-RUN: show everything, change nothing.
        if json_out:
            import json

            print(json.dumps(_render_json(
                card, project, branch, facts, verify_present, verify_text,
                projects=projects, landing=False,
            )))
            return 0
        for line in _render(card, project, branch, facts, verify_present, verify_text, projects):
            print(line)
        print("\ndry-run: pass --apply to merge the branch and close the card.")
        return 0

    # --apply: merge the branch AND close the card as ONE action.
    outcome = _do_apply(repo, branch, base, _run)
    if outcome.startswith("merge failed") or outcome.startswith("merge done"):
        # On a failed merge there is nothing to close — the card stays open so
        # the operator can resolve and retry. Only a landed merge closes it.
        if json_out:
            import json

            print(json.dumps(_render_json(
                card, project, branch, facts, verify_present, verify_text,
                projects=projects, landing=False, apply_outcome=outcome,
            )))
            return 1
        for line in _render(
            card, project, branch, facts, verify_present, verify_text,
            projects, apply_outcome=outcome,
        ):
            print(line)
        return 1

    # Close the card ONLY now that the merge has landed. A ``False`` return
    # (archive genuinely failed, e.g. the row no longer matches) is surfaced
    # as a clear warning rather than claiming success unconditionally — the
    # exact bug this module historically shipped. An injected fake that returns
    # ``None`` (old-style "record the call" seam) is treated as success; only an
    # explicit ``False`` means the archive failed.
    close_ok = True
    if _close_card is not None:
        result = _close_card(card_id, card.get("board"))
        close_ok = True if result is None else bool(result)

    if json_out:
        import json

        print(json.dumps(_render_json(
            card, project, branch, facts, verify_present, verify_text,
            projects=projects, landing=False, apply_outcome=outcome,
        )))
        return 0
    for line in _render(
        card, project, branch, facts, verify_present, verify_text,
        projects, apply_outcome=outcome,
    ):
        print(line)
    if close_ok:
        print(f"card {card_id} closed")
    else:
        print(
            f"warning: card {card_id} merged but could not be archived — "
            f"the card may already be archived. Run `flightdeck reconcile "
            f"--apply` or archive it manually.",
            file=sys.stderr,
        )
    return 0


# --------------------------------------------------------------------------- #
# --queue — everything genuinely awaiting review, newest first
# --------------------------------------------------------------------------- #

def _format_age(seconds) -> str:
    """Render an age in seconds as a short human string, or 'unknown'."""
    if seconds is None:
        return "unknown"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    days = hours // 24
    if days < 30:
        return f"{days}d"
    return f"{days // 30}mo"


def _enrich_project_cards(projects, _run) -> list[dict]:
    """Read each project's board cards and attach git facts per card.

    For every project with a board, reads that board's cards via
    :func:`kanban.list_cards` and computes ``branch_exists`` / ``is_merged``
    for each card's branch against that project's repo with git_state. A
    project with no board contributes nothing (there is no card source). A
    board that cannot be read contributes nothing rather than a guess.
    """
    enriched: list[dict] = []
    for proj in projects:
        board = proj.board
        if not board:
            continue
        try:
            cards = kanban.list_cards(board=board)
        except kanban.KanbanError:
            # Cannot read this board; surface nothing rather than guessing.
            continue
        for card in cards:
            branch = card.get("branch")
            d = dict(card)
            d["project"] = proj.name
            if branch:
                d["branch_exists"] = git_state.branch_exists(proj.repo, branch, _run=_run)
                d["is_merged"] = git_state.is_merged(proj.repo, branch, "main", _run=_run)
            else:
                d["branch_exists"] = False
                d["is_merged"] = False
            enriched.append(d)
    return enriched


def cmd_queue(args: argparse.Namespace) -> int:
    projects = registry.load_registry(args.registry)
    enriched = _enrich_project_cards(projects, args.run)
    rows = review.review_queue(enriched, now=args.now)

    if args.json:
        import json

        print(
            json.dumps(
                [
                    {
                        "project": r["project"],
                        "card_id": r["card_id"],
                        "branch": r["branch"],
                        "age_seconds": r["age_seconds"],
                        "title": r["title"],
                    }
                    for r in rows
                ]
            )
        )
        return 0

    if not rows:
        print("review queue: no cards awaiting review.")
        # A watermark still tells the operator how fresh the (empty) read is.
        _print_watermark(enriched, _run=args.run)
        return 0

    for r in rows:
        age = _format_age(r["age_seconds"])
        print(
            f"{r['project']:<20} {r['card_id']:<16} {r['branch']:<28} "
            f"{age:>7}  {r['title']}"
        )
    print(f"\n{len(rows)} card(s) awaiting review.")
    _print_watermark(enriched, _run=args.run)
    return 0


def _print_watermark(enriched, *, _run=None) -> None:
    """Print a ``data as of <time>`` freshness line for the queue.

    Dates the OLDEST critical input behind the queue: the freshest state-change
    each contributing board recorded, floored to the oldest of them (see
    :func:`kanban.freshness_watermark`). The contributing boards are exactly
    those that yielded cards this pass; an empty/quiet board never lowers it.
    A board we could not read dates nothing. The line is a factual date, not a
    staleness verdict — the per-row ages already date each card, and a loud
    stale alert is out of scope here (standup's coverage footer owns that).
    """
    boards = sorted({str(d.get("board")) for d in enriched if d.get("board")})
    if not boards:
        return
    try:
        watermarks = kanban.list_board_watermarks(boards=boards)
    except kanban.KanbanError:
        print("data as of <unknown>")
        return
    watermark = kanban.freshness_watermark(boards, watermarks=watermarks or {})
    if not isinstance(watermark, (int, float)) or int(watermark) <= 0:
        print("data as of <unknown>")
        return
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(watermark)))
    print(f"data as of {when} ({_format_age(int(time.time()) - int(watermark))} ago)")


# --------------------------------------------------------------------------- #
# <project> — run verify + apply the test-quality GATE before merge
# --------------------------------------------------------------------------- #

def cmd_verify(args: argparse.Namespace, proj: registry.Project) -> int:
    baseline = BaselineStore(args.baseline)
    result = review.run_verify_with_gate(
        proj,
        baseline,
        _run=args.run,
        _now=args.now_fn,
    )

    if result.returncode == 127:
        print(
            f"review {proj.name}: no 'verify' command in the registry for this "
            f"project; nothing to gate.",
            file=sys.stderr,
        )
        return 2

    if args.json:
        import json

        print(
            json.dumps(
                {
                    "project": result.project,
                    "passed": result.returncode == 0,
                    "returncode": result.returncode,
                    "total_seconds": result.total_seconds,
                    "baseline_seconds": result.baseline_seconds,
                    "tests": [
                        {"name": t.name, "duration": t.duration, "phase": t.phase}
                        for t in result.tests
                    ],
                    "flags": result.flags,
                }
            )
        )
    else:
        status = "PASS" if result.returncode == 0 else f"FAIL (rc={result.returncode})"
        total = (
            f"{result.total_seconds:.2f}s" if result.total_seconds is not None else "unmeasured"
        )
        base = (
            f"{result.baseline_seconds:.2f}s"
            if result.baseline_seconds is not None
            else "none"
        )
        print(f"review {result.project}: verify {status}  suite {total}  baseline {base}")
        for t in result.tests:
            print(f"    {t.duration:7.2f}s {t.name}")
        if result.flags:
            print("BEFORE MERGE:")
            for flag in result.flags:
                print(f"    [FLAG] {flag}")
        else:
            print("test-quality gate: clean — no flags before merge.")

    return 0 if result.ok and result.returncode == 0 else 1


# --------------------------------------------------------------------------- #
# Dispatch from the CLI entry
# --------------------------------------------------------------------------- #

def cmd_dispatch(args: argparse.Namespace) -> int:
    """Route `review` to queue / card / verify based on how it was invoked.

    * ``review --queue`` — the awaiting-review queue.
    * ``review <thing>`` — the positional is either a card id (card review)
      or a project name (verify + gate). Dispatch is explicit and never
      guessed: a positional that matches a known card id follows F9a's card
      flow; otherwise it is treated as a project name (verify + gate).
    * ``review`` with no positional — when the cwd resolves to a registered
      project, run that project's verify+gate (printing the detection note);
      otherwise say there is nothing to do, exactly as before.
    """
    if args.queue:
        return cmd_queue(args)

    token = args.card
    if not token:
        # Auto-detect: no positional and cwd inside a registered repo -> that
        # project's verify+gate. A cwd outside every repo falls through to the
        # unchanged "nothing to do" message.
        projects = registry.load_registry(args.registry)
        detected, _detected = registry.resolve_project_arg(
            projects, None,
            cwd=getattr(args, "cwd", None),
            _print=lambda line: print(line, file=sys.stderr),
        )
        if detected:
            proj = registry.get_project(detected, path=args.registry)
            return cmd_verify(args, proj)
        print(
            "review: nothing to do. Try `flightdeck review --queue`, "
            "`flightdeck review <card-id>`, or `flightdeck review <project>`.",
            file=sys.stderr,
        )
        return 2

    # Is the positional a card id? Compare against the loaded cards (string
    # compare, same rule as core.review._find_card). Only when it is NOT a
    # card id do we fall through to project (verify+gate) resolution.
    cards = getattr(args, "cards", None) or []
    if any(str(c.get("id")) == token for c in cards):
        return cmd_review(args)

    try:
        proj = registry.get_project(token, path=args.registry)
    except registry.ProjectNotFoundError:
        print(
            f"error: {token!r} is neither a known card id nor a registry project.",
            file=sys.stderr,
        )
        return 2

    return cmd_verify(args, proj)


def build_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "review",
        help="review a card, list the awaiting-review queue, or gate a verify run",
        epilog="example: flightdeck review t_942dcbd1 --apply",
    )
    p.add_argument(
        "--queue",
        action="store_true",
        help="list everything genuinely awaiting review, newest first",
    )
    p.add_argument(
        "card",
        nargs="?",
        metavar="CARD_ID_OR_PROJECT",
        help="kanban card id (e.g. t_abc123) to review, or a project to verify+gate",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="merge the branch into main AND close the card (one action)",
    )
    p.add_argument(
        "--base",
        default=DEFAULT_BASE,
        help="base branch the card merges into (default: %(default)s)",
    )
    p.add_argument(
        "--baseline",
        default=None,
        metavar="PATH",
        help="test-baseline file (default ~/.flightdeck/test-baseline.yaml)",
    )
    p.set_defaults(func=cmd_dispatch)


def run(args: argparse.Namespace, registry_path: str) -> int:
    """Entry from cli.py. Attaches the injectable seams to ``args``.

    ``cards`` (the list of kanban cards) is read here so the dispatcher and
    ``cmd_review`` can be driven in tests with a stubbed set, but ``run``
    itself reads the live board through core/kanban (which is itself
    injectable inside core, not here). Also attaches the queue/gate seams:
    ``now`` (an int clock driving the queue's age) and ``now_fn`` (a callable
    driving baseline timestamps).
    """
    import time

    args.registry = registry_path
    args.run = getattr(args, "run", None)
    args.close_card = getattr(args, "close_card", None)
    if args.close_card is None:
        # Real CLI path: wire the genuine archiving implementation. Tests that
        # want a fake inject their own `close_card` before calling run() and it
        # is preserved here — only a None (never-injected) value falls through
        # to the real one. This is the fix: before, nothing in production ever
        # set close_card, so `review --apply` claimed "card closed" without
        # archiving anything.
        args.close_card = _real_close_card
    args.base = getattr(args, "base", DEFAULT_BASE) or DEFAULT_BASE
    args.now = getattr(args, "now", None)
    if args.now is None:
        args.now = int(time.time())
    args.now_fn = getattr(args, "now_fn", None)
    args.baseline = getattr(args, "baseline", None)
    args.cwd = getattr(args, "cwd", None)
    if getattr(args, "cards", None) is None:
        try:
            args.cards = kanban.list_cards()
        except kanban.KanbanError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    func = getattr(args, "func", None)
    if func is None:
        print(
            "review: nothing to do. Try `flightdeck review --queue`, "
            "`flightdeck review <card-id>`, or `flightdeck review <project>`.",
            file=sys.stderr,
        )
        return 2
    return func(args)
