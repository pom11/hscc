"""kanban.py — read Hermes kanban cards and apply the reconcile rules.

This is the integrity core of flightdeck. It does two things:

1. :func:`list_cards` — read cards from Hermes' own kanban library
   (``hermes_cli.kanban_db``), never by shelling out and parsing text.
2. :func:`classify` / :func:`reconcile_plan` — decide what each card
   means *given git facts*, and assemble a reconciliation PLAN.

The git facts come from :mod:`flightdeck.core.git_state`, injected by the
caller. This module never touches git, the network, or the live board — it
is pure decision logic over two inputs: the cards and their git facts. The
board is never mutated here; closing / archiving is the ``reconcile``
command's job, acting on the plan we return.

Why this module exists (the measured failure behind the DESIGN):

- Work flows card -> branch -> blocked-for-review -> human merges -> **the
  card stays blocked forever**. Nothing reconciles the board against git.
- Before flightdeck, 28 cards showed as awaiting review; zero needed it.
  14 had branches already merged; the "NEEDS YOU" signal was 89-100% false.

The rule that makes the signal true: a card only classifies as
``needs_you`` if its branch is *genuinely unmerged*. A merged branch always
wins and classifies ``landed`` (the card should be closed).
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

import yaml

# Hermes board slugs come pre-normalized (lowercase, hyphens) from the
# library; the registry stores the same slug. No transformation needed.
DEFAULT_BOARD = "default"

# Statuses that mean "the operator must review this work". Hermes uses
# ``review`` and ``blocked``.
REVIEW_STATUSES = frozenset({"review", "blocked"})

# Statuses that mean "a worker has claimed this and is (supposed to be) doing
# it". Only cards actually in flight can go stale.
IN_FLIGHT_STATUSES = frozenset({"running"})

# Statuses that mean a worker is actively engaged right now. A card in one of
# these is never closed, no matter what its branch looks like — the worker may
# be mid-run and its branch may merely look "merged" because main advanced
# past the fork point without the card producing any commits yet.
ACTIVE_STATUSES = frozenset({"running", "claimed"})

# Statuses that are SETTLED — the work is finished and needs neither operator
# attention nor is it in flight. A card in one of these must never surface in
# a digest or reconciliation as running/needs-attention. Hermes' own
# ``list_tasks`` already excludes ``archived`` from its default read; this
# extends the same "settled" logic to ``done``, which ``list_tasks`` still
# returns but which the digest must not report as in-flight. See
# :func:`list_cards` and the standup stale-status bug it fixes.
SETTLED_STATUSES = frozenset({"done", "archived"})

# Default thresholds (all injectable via classify / reconcile_plan kwargs).
DEFAULT_STALE_THRESHOLD_SECONDS = 45 * 60        # 45m, per DESIGN
DEFAULT_DEAD_DAYS = 14                            # unstarted for 2 weeks
DEFAULT_HEARTBEAT_WINDOW_SECONDS = 30 * 60        # recent heartbeat = actively running

# The branch convention. When a card carries no explicit branch_name, the
# work lives on a branch derived from the card id (see the worktrees the
# dispatcher creates: wt/<card_id>).
BRANCH_PREFIX = "wt/"

# Nominal location of the Hermes agent source tree, used to import its kanban
# library. Overridable via env so tests and unusual installs can point at it.
_HERMES_AGENT_PATH = "~/.hermes/hermes-agent"


class KanbanError(Exception):
    """Base error for kanban reading. Raised (rarely) when Hermes is not
    installed so the caller can surface a clear message instead of an obscure
    ImportError traceback."""


def _load_kanban_db() -> Any:
    """Import Hermes' ``hermes_cli.kanban_db`` module, raising a clear error
    when Hermes is not installed.

    The import is deliberately kept inside this function (never at module
    top level) so tests can stub it, and so importing this module never
    depends on Hermes being present. The agent source tree is put on
    ``sys.path`` only for the duration of this one import.
    """
    hermes_path = os.path.expanduser(
        os.environ.get("HERMES_AGENT_PATH", _HERMES_AGENT_PATH)
    )
    if not os.path.isdir(hermes_path):
        raise KanbanError(
            f"Hermes agent source not found at {hermes_path!r}; cannot read the "
            f"kanban board. Set HERMES_AGENT_PATH if it lives elsewhere."
        )
    if hermes_path not in sys.path:
        sys.path.insert(0, hermes_path)
    try:
        from hermes_cli import kanban_db  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - defensive
        raise KanbanError(
            f"could not import hermes_cli.kanban_db from {hermes_path!r}: {exc}"
        ) from exc
    return kanban_db


def _card_branch(card: dict) -> str:
    """The branch a card's work lives on.

    Uses the card's recorded ``branch`` when present, otherwise the
    convention ``wt/<card_id>``. A branch is always a str (never None) so
    downstream code can compare it directly.
    """
    branch = card.get("branch")
    if branch:
        return str(branch)
    return f"{BRANCH_PREFIX}{card.get('id', '')}"


def _facts_for(git_facts: dict, card_id: Optional[str]) -> dict:
    """The git facts for one card, defaulting every field to its safe value.

    A missing entry (or missing keys) defaults to the *conservative* reading:
    branch doesn't exist, not merged, zero commits. Conservative means we will
    never claim work landed or needs review that we could not verify — the
    ``never report a state you have not verified`` principle.
    """
    if card_id is None:
        return {
            "branch_exists": False,
            "is_merged": False,
            "commits_ahead": 0,
            "own_commits": 0,
            "landed_via_merge": False,
        }
    facts = git_facts.get(card_id)
    if not isinstance(facts, dict):
        return {
            "branch_exists": False,
            "is_merged": False,
            "commits_ahead": 0,
            "own_commits": 0,
            "landed_via_merge": False,
        }
    out = {
        "branch_exists": bool(facts.get("branch_exists", False)),
        "is_merged": bool(facts.get("is_merged", False)),
        "commits_ahead": int(facts.get("commits_ahead", 0) or 0),
        "own_commits": int(facts.get("own_commits", 0) or 0),
        "landed_via_merge": bool(facts.get("landed_via_merge", False)),
    }
    return out


def _older_than(
    card: dict, seconds: int, *, field: str, now: Optional[int] = None
) -> bool:
    """True if the card's ``field`` epoch timestamp is older than ``seconds``.

    A missing / invalid timestamp is not "older than" anything — we only
    flag staleness on a signal we can actually see, never on a guess.
    """
    ts = card.get(field)
    if not ts:
        return False
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return False
    if now is None:
        now = int(time.time())
    return (now - ts) > seconds


def _recent(
    card: dict, seconds: int, *, field: str, now: Optional[int] = None
) -> bool:
    """True if the card's ``field`` epoch timestamp is within the last ``seconds``.

    The complement of :func:`_older_than`: a recent value (heartbeat, started_at)
    means the item is actively engaged. A missing / invalid / non-numeric
    timestamp is never "recent" — we only treat a signal we can actually see as
    recent. This guards the close path: a card with a recent heartbeat is
    actively running and must never be closed.
    """
    ts = card.get(field)
    if not ts:
        return False
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return False
    if now is None:
        now = int(time.time())
    return (now - ts) <= seconds


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_boards() -> list[str]:
    """Return the slug of every board known to Hermes on this host.

    Reads directly through Hermes' kanban library (``kanban_db.list_boards``),
    so board discovery is owned here and never reimplemented by a command. The
    board slug is already the pre-normalized project key: no transformation is
    applied.
    """
    kdb = _load_kanban_db()
    return [str(entry["slug"]) for entry in kdb.list_boards()]


def board_workdirs() -> dict[str, Optional[str]]:
    """Map every board slug -> its configured ``default_workdir`` (or None).

    Complements :func:`list_boards`, which returns only slugs. Both read the
    same Hermes library snapshot, so doctor can compare a project's repo to
    the board its work should land in. A board with no ``default_workdir``
    (e.g. the pre-existing ``default`` and ``hscc`` boards) maps to ``None``.
    """
    kdb = _load_kanban_db()
    return {
        str(entry["slug"]): entry.get("default_workdir")
        for entry in kdb.list_boards()
    }


def board_card_count(board: str) -> int:
    """Number of (non-archived) cards on a board, used for orphan reporting."""
    return len(list_cards(board=board))


def create_board(
    slug: str,
    *,
    name: str | None = None,
    description: str | None = None,
    icon: str | None = None,
    color: str | None = None,
    default_workdir: str | None = None,
    _kdb=None,
) -> None:
    """Create a new Hermes board (a separate SQLite file).

    Thin wrapper over ``kanban_db.create_board`` so board creation is owned
    here (one seam for all board access), like :func:`list_boards`. Setting
    ``default_workdir`` to a project repo is what makes new cards land in that
    project's worktree.

    Creating a board NEVER reads, moves or modifies a single existing card —
    a new board is an empty DB with no cards on it. Migrating historical cards
    out of ``default`` is deliberately out of scope.

    ``_kdb`` is the injectable kanban library (a fake in tests) so board
    creation never touches the live board files during the test suite.
    """
    kdb = _kdb if _kdb is not None else _load_kanban_db()
    kdb.create_board(
        slug,
        name=name,
        description=description,
        icon=icon,
        color=color,
        default_workdir=default_workdir,
    )


def list_cards(
    board: Optional[str] = None,
    *,
    include_archived: bool = False,
) -> list[dict]:
    """Return flightdeck card dicts for one board (or all boards).

    Reads the board(s) directly through Hermes' kanban library. Each returned
    dict carries at least: ``id``, ``title``, ``status``, ``assignee``,
    ``board`` (the slug it came from), and ``branch`` (the recorded branch, or
    the ``wt/<id>`` convention). Extra fields (``created_at``, ``started_at``,
    ``completed_at``, ``workspace_kind``) are included because classify uses
    them for the dead/stale age checks.

    ``board=None`` reads **every** board on the host (each board is a separate
    DB); ``board="slug"`` reads only that one. Settled cards (status ``done``
    or ``archived``) are excluded unless ``include_archived=True`` — a settled
    card is already finished and not part of any reconciliation or digest.
    ``archived`` is excluded by Hermes' own ``list_tasks``; ``done`` is
    excluded here (:data:`SETTLED_STATUSES`) so a completed card is never
    reported as running/needs-attention.
    """
    kdb = _load_kanban_db()

    slugs: list[str]
    if board is not None:
        slugs = [str(board)]
    else:
        slugs = [entry["slug"] for entry in kdb.list_boards()]

    cards: list[dict] = []
    for slug in slugs:
        conn = kdb.connect(board=slug)
        try:
            tasks = kdb.list_tasks(conn, include_archived=include_archived)
        finally:
            conn.close()
        for t in tasks:
            # Default read: skip settled cards (done/archived). Hermes already
            # filters ``archived`` at the SQL level; ``done`` is filtered here
            # so a completed-but-not-archived card is never surfaced as
            # running/needs-attention by a digest or reconciliation.
            if not include_archived and str(getattr(t, "status", "") or "") in SETTLED_STATUSES:
                continue
            cards.append(_to_card(t, slug))
    return cards


# --------------------------------------------------------------------------- #
# Freshness watermark — "data as of <time>" for read commands
# --------------------------------------------------------------------------- #
#
# Every digest command reads boards and git and reports a digest, but nothing
# historically told the operator HOW FRESH that data is. A cached/stale board
# read looks identical to a current one. These helpers give read commands a
# per-board "newest observed state change" timestamp plus a digest-level
# watermark computed from the OLDEST critical input, so a board that fell
# behind can never hide behind a digest that claims everything is current.
#
# What "stale" means per source — the quiet-is-not-stale judgment:
#
#   A board's freshness is the newest state-change it recorded: the newest
#   ``task_events.created_at``, else the newest card-level update timestamp
#   (``last_heartbeat_at`` -> ``completed_at`` -> ``started_at`` ->
#   ``created_at``). A board with genuinely no recent activity reads as an old
#   timestamp — that is being QUIET, not stale, and quietness alone is never
#   alarmed on. The digest watermark is the MINIMUM (oldest) freshness across
#   only the boards that CONTRIBUTED cards to the digest: a board that fed no
#   card (an empty board) is quiet by definition and excluded, so its silence
#   can never drag the watermark down. A non-empty board whose newest change
#   is old DOES lower the watermark, which is correct — the digest's board
#   data is genuinely that old. The watermark is a factual DATE ("data as of
#   X"), never a verdict: it does not scream "STALE!" at an arbitrary age
#   threshold (unknowable per-project; would false-alarm on legitimate
#   long-running quiet cards). The distinct, louder stale alert remains
#   reserved for read FAILURES, which commands already surface separately
#   (e.g. UNREADABLE in standup).
#
# git refs (branch existence / merged / commits-ahead) are read live from each
# repo at digest time and are therefore inherently as fresh as the moment of
# the read — they never need a watermark. The freshness dimension that can go
# stale is the BOARD, which is what these helpers date.


def list_board_watermarks(*, _kdb=None, boards=None) -> dict[str, int]:
    """Map board slug -> the newest state-change timestamp it recorded (epoch).

    For each live board, the freshness is the newest ``task_events.created_at``
    in that board's DB, falling back to the newest card-level update timestamp
    (``last_heartbeat_at`` -> ``completed_at`` -> ``started_at`` ->
    ``created_at``) when the board holds no events at all. This is the "last
    time the board's data actually changed" — the signal a read command needs
    to date its digest.

    A board with NO cards and NO events returns no entry (absent, not ``0``):
    an empty board is quiet by definition and is not a stale critical input
    (see the module note above). ``boards`` restricts the read to specific
    slugs (a command that already knows which boards back it can pass the set
    it actually read); ``None`` reads every live board.

    ``_kdb`` is the injectable kanban library (a fake in tests) so a watermark
    read never touches the live board during the test suite.
    """
    kdb = _kdb if _kdb is not None else _load_kanban_db()
    if boards is not None:
        slugs = [str(b) for b in boards]
    else:
        slugs = [str(e["slug"]) for e in kdb.list_boards()]

    watermarks: dict[str, int] = {}
    for slug in slugs:
        conn = kdb.connect(board=slug)
        try:
            row = conn.execute(
                "SELECT MAX(ts) FROM ("
                "  SELECT created_at AS ts FROM tasks"
                "  UNION ALL SELECT started_at AS ts FROM tasks"
                "    WHERE started_at IS NOT NULL"
                "  UNION ALL SELECT completed_at AS ts FROM tasks"
                "    WHERE completed_at IS NOT NULL"
                "  UNION ALL SELECT last_heartbeat_at AS ts FROM tasks"
                "    WHERE last_heartbeat_at IS NOT NULL"
                "  UNION ALL SELECT created_at AS ts FROM task_events"
                ")"
            ).fetchone()
        except Exception:  # noqa: BLE001 - a board we cannot read dates nothing
            continue
        finally:
            conn.close()
        ts = row[0] if row else None
        if isinstance(ts, (int, float)) and int(ts) > 0:
            watermarks[slug] = int(ts)
    return watermarks


def freshness_watermark(
    contributing_boards,
    *,
    watermarks: Optional[dict] = None,
    _kdb=None,
) -> Optional[int]:
    """The digest watermark: the OLDEST freshness among the boards that fed it.

    ``contributing_boards`` is the iterable of board slugs that actually
    contributed cards to the digest (the set its read pass observed). The
    watermark is the minimum (oldest) per-board freshness among those boards —
    the ``data as of <time>`` floor: if any one board behind the digest is
    stale, the digest says so. Args:

    * ``watermarks`` — an already-computed ``{slug: ts}`` map (e.g. from
      :func:`list_board_watermarks`). When provided it is used and ``_kdb`` is
      ignored, so a command can read watermarks once and reuse them.
    * ``_kdb`` — when ``watermarks`` is None, the kanban library used to read
      them fresh (an injectable fake in tests).

    Returns ``None`` when no contributing board has a readable timestamp (the
    whole digest is one quiet/empty source set) — the caller renders that as
    ``data as of <unknown>`` rather than inventing a number. Empty/quiet
    boards that contributed nothing never lower the watermark.
    """
    if watermarks is None:
        watermarks = list_board_watermarks(_kdb=_kdb)
    oldest: Optional[int] = None
    for board in contributing_boards:
        ts = watermarks.get(str(board))
        if ts is None:
            continue
        if oldest is None or ts < oldest:
            oldest = ts
    return oldest


def _to_card(task: Any, board: str) -> dict:
    """Map a Hermes ``Task`` object to a flightdeck card dict.

    The ``branch`` field carries the resolved branch: the task's recorded
    ``branch_name`` when present, otherwise the ``wt/<id>`` convention.
    ``priority`` is carried through so a card-rehoming command can preserve
    the source card's priority (the schema has one).
    """
    card = {
        "id": task.id,
        "title": task.title,
        "body": getattr(task, "body", None),
        "status": task.status,
        "assignee": task.assignee,
        "board": board,
        "branch": task.branch_name,   # overwritten below with the fallback
        "priority": getattr(task, "priority", 0),
        "created_at": task.created_at,
        "started_at": task.started_at,
        "completed_at": task.completed_at,
        "last_heartbeat_at": getattr(task, "last_heartbeat_at", None),
        "workspace_kind": task.workspace_kind,
        "workspace_path": getattr(task, "workspace_path", None),
    }
    if not card["branch"]:
        card["branch"] = f"{BRANCH_PREFIX}{task.id}"
    return card


# --------------------------------------------------------------------------- #
# Archived board read path — separate, explicitly-invoked (never the default)
# --------------------------------------------------------------------------- #
#
# ``list_boards`` / ``list_cards`` discover only live boards and skip archived
# ones, because scanning archive history on every run is expensive and rarely
# wanted. When an operator DOES want to review what got archived (the
# `legacy-cards --include-archived-boards` command), they read through these
# functions on an explicit call. Each archived board was moved by Hermes to
# ``<root>/_archived/<slug>-<timestamp>/kanban.db`` (see kanban_db.remove_board),
# so this path is a directory scan + a connect to each db — always read-only.


def _archived_boards_root(_kdb=None) -> str:
    """The on-disk ``_archived/`` root, or '' when Hermes has no such dir.

    ``_kdb`` is the injectable kanban library (a fake in tests). An absent root
    is '' (the caller treats it as \"nothing archived\"), never an error — a host
    with no archives must degrade to no extra scanning.
    """
    kdb = _kdb if _kdb is not None else _load_kanban_db()
    try:
        root = kdb.boards_root() / "_archived"
    except Exception:  # noqa: BLE001 - a Hermes without a boards_root has no archives
        return ""
    return str(root)


def list_archived_board_sources(*, _kdb=None, _archived_root=None) -> list[tuple[str, str]]:
    """``(board_label, db_path)`` for every archived board directory.

    Read-only filesystem scan of Hermes' ``_archived/`` root. Each archived
    board is a directory holding a ``kanban.db``; the label is the directory's
    name (e.g. ``ecofire-1786704010``) so a reviewer can tell one archived board
    apart from another. An absent/empty root is an empty list — never an error.
    ``_archived_root`` overrides the scan root (tests); ``_kdb`` is the kanban
    library (a fake in tests) whose ``boards_root()`` locates the default.
    """
    root = _archived_root if _archived_root is not None else _archived_boards_root(_kdb=_kdb)
    if not root:
        return []
    root_path = os.path.expanduser(str(root))
    if not os.path.isdir(root_path):
        return []
    sources: list[tuple[str, str]] = []
    for name in sorted(os.listdir(root_path)):
        child = os.path.join(root_path, name)
        if not os.path.isdir(child):
            continue
        db = os.path.join(child, "kanban.db")
        if os.path.isfile(db):
            sources.append((name, db))
    return sources


def list_archived_board_cards(*, _kdb=None, _sources=None, _archived_root=None) -> list[dict]:
    """Cards from every archived board directory, labelled by archive dir.

    The separate, explicitly-invoked read path for Hermes' ``_archived/`` boards
    (which ``list_boards`` / ``list_cards`` do not discover). Read-only: it only
    connects to and reads each archived db. ``_sources`` replaces the filesystem
    scan (tests); ``_archived_root`` overrides the scan root; ``_kdb`` is the
    kanban library (a fake in tests) used to open each db.
    """
    kdb = _kdb if _kdb is not None else _load_kanban_db()
    sources = _sources if _sources is not None else list_archived_board_sources(
        _kdb=kdb, _archived_root=_archived_root
    )
    cards: list[dict] = []
    for board_label, db_path in sources:
        conn = kdb.connect(db_path=Path(db_path))
        try:
            tasks = kdb.list_tasks(conn, include_archived=True)
        finally:
            conn.close()
        for t in tasks:
            card = _to_card(t, board_label)
            # Carry the full db path so a caller that must OPEN the archived
            # board (e.g. migrate-card archiving the original card) can connect
            # directly — an archived slug cannot be resolved by connect(board=).
            card["_board_path"] = str(db_path)
            cards.append(card)
    return cards


# --------------------------------------------------------------------------- #
# One card's full story — the `why <card>` read path
# --------------------------------------------------------------------------- #
#
# ``list_cards`` reads every card on a board; ``find_card`` reads exactly one
# across every board. The distinction matters for `why`: it wants ONE card's
# complete picture (identity + timing + branch + workspace) and must be able to
# say loudly which boards it searched when the card is not found. These helpers
# keep all board + event access owned here — one seam — so the command layer
# never reimplements board reading.

# Event kinds that mark a transition into a NEW status. ``why`` uses the most
# recent such event as when the card entered its current status (for "how long
# in this status"). Non-transition events (commented, heartbeat, spawned,
# assigned, edited, attachment ops) do NOT advance the "current status" clock.
STATUS_TRANSITION_EVENTS = frozenset(
    {
        "created",
        "promoted",
        "claimed",
        "submitted_for_review",
        "blocked",
        "gave_up",
        "dependency_wait",
        "unblocked",
        "completed",
        "archived",
        "reclaimed",
        "scheduled",
    }
)


def find_card(card_id: str, *, _kdb=None, boards: Optional[list] = None):
    """The card dict for ``card_id`` across boards, or None when not found.

    Reads every board (or an explicit ``boards`` list) and returns the first
    non-archived card whose id matches, as a flightdeck card dict via
    :func:`_to_card` (so ``last_heartbeat_at`` is included). When the card is
    not on any live board, falls back to scanning the archived boards via
    :func:`list_archived_board_cards` (the SAME reader `legacy-cards` uses) —
    so a card surfaced there can be acted on. An archived-board hit returns
    the archived card dict with ``_board_path`` set (the db path), because an
    archived slug like ``ecofire-1786704010`` cannot be resolved by
    ``connect(board=...)`` — the caller opens it via ``db_path`` directly.

    The archived scan is intentionally a fallback, only run when the live scan
    misses, so normal (live-board) lookups pay no extra cost. Returns ``None``
    when no board holds the card — the caller surfaces the error naming the
    boards searched. ``_kdb`` is the injectable kanban library (a fake in
    tests) so looking up a card never touches the live board.
    """
    kdb = _kdb if _kdb is not None else _load_kanban_db()
    if boards is not None:
        slugs = [str(b) for b in boards]
    else:
        slugs = [str(e["slug"]) for e in kdb.list_boards()]
    for slug in slugs:
        conn = kdb.connect(board=slug)
        try:
            tasks = kdb.list_tasks(conn, include_archived=False)
        finally:
            conn.close()
        for t in tasks:
            if str(t.id) == str(card_id):
                return _to_card(t, slug)
    # Live scan missed — fall back to archived boards (opt-in, expensive, so
    # only when the fast path fails).
    for acard in list_archived_board_cards(_kdb=kdb):
        if str(acard.get("id")) == str(card_id):
            return acard
    return None


def list_boards_searched(*, _kdb=None, boards: Optional[list] = None) -> list[str]:
    """The board slugs a :func:`find_card` lookup would (or did) cover.

    Returns the resolved ``boards`` argument, or every board slug known to
    Hermes. A card-not-found error names these so the operator knows exactly
    where the lookup looked. ``_kdb`` is injectable (a fake in tests).
    """
    if boards is not None:
        return [str(b) for b in boards]
    kdb = _kdb if _kdb is not None else _load_kanban_db()
    return [str(e["slug"]) for e in kdb.list_boards()]


def card_events(
    card_id: str,
    *,
    _kdb=None,
    boards: Optional[list] = None,
) -> list[dict]:
    """The event timeline for ``card_id`` as ``[{kind, created_at}, ...]``.

    Searches every board (or an explicit ``boards`` list) for the card and
    returns its events via ``kanban_db.list_events``, ordered ascending by
    ``created_at``. Returns ``[]`` when the card cannot be found on any board
    (or the board cannot be read) — the no-data reading is an empty list, never
    an exception, so a metrics run over a board we cannot read degrades to
    "insufficient data" rather than crashing.

    ``_kdb`` is the injectable kanban library (a fake in tests) so reading a
    card's events never touches the live board during the test suite.
    """
    kdb = _kdb if _kdb is not None else _load_kanban_db()
    if boards is not None:
        slugs = [str(b) for b in boards]
    else:
        slugs = [str(e["slug"]) for e in kdb.list_boards()]
    for slug in slugs:
        conn = kdb.connect(board=slug)
        try:
            tasks = kdb.list_tasks(conn, include_archived=True)
            found = any(str(t.id) == str(card_id) for t in tasks)
            if not found:
                continue
            events = kdb.list_events(conn, str(card_id))
        finally:
            conn.close()
        return [
            {"kind": getattr(e, "kind", None), "created_at": getattr(e, "created_at", None)}
            for e in events
        ]
    return []


def status_changed_at(
    card: dict,
    events: Optional[list] = None,
    *,
    now: Optional[int] = None,
) -> Optional[int]:
    """The epoch timestamp when the card entered its CURRENT status, or None.

    The canonical source is the most recent status-transition event (kinds in
    :data:`STATUS_TRANSITION_EVENTS`) — that is when the current status began.
    Events are expected in ascending ``created_at`` order, so the LAST
    transition event wins. When no transition event is available (``events``
    empty/None), falls back to ``started_at``, then ``created_at`` — the best
    available proxy for "the current state began here". Never negative and
    never in the future (clamped). Returns None only when the card has no
    usable time at all.
    """
    events = events or []
    transition_ts = None
    for ev in events:
        if not isinstance(ev, dict):
            continue
        if ev.get("kind") in STATUS_TRANSITION_EVENTS:
            ts = ev.get("created_at")
            if ts:
                transition_ts = int(ts)
    if transition_ts is None:
        for field in ("started_at", "created_at"):
            ts = card.get(field)
            if ts:
                transition_ts = int(ts)
                break
    if transition_ts is None:
        return None
    if now is None:
        now = int(time.time())
    return max(0, min(int(now), transition_ts))


def status_duration_seconds(
    card: dict,
    events: Optional[list] = None,
    *,
    now: Optional[int] = None,
) -> Optional[int]:
    """How many seconds the card has been in its CURRENT status, or None.

    ``now - status_changed_at`` where :func:`status_changed_at` resolves the
    status-transition time (most recent transition event, else started_at,
    else created_at). None when no usable time exists — rendered as "unknown" —
    never a fabricated 0.
    """
    changed = status_changed_at(card, events, now=now)
    if changed is None:
        return None
    now = int(time.time()) if now is None else int(now)
    return max(0, now - changed)


# The shared ``default`` board holds cards from MANY projects, so a card's
# board slug cannot tell us which project it belongs to. The field that does is
# ``workspace_path``: the checkout the dispatcher gave that card, which is the
# project repo itself or a worktree under it (``<repo>/.worktrees/<card>``). A
# card therefore belongs to a project when its workspace_path IS that project's
# repo or lives UNDER it, after both are resolved and normalised. Board remains
# an optional NARROWING filter on which board(s) we read — never the attribution
# rule itself.


class Unattributed:
    """Sentinel returned by :func:`project_for_card` when a card cannot be
    attributed to any project.

    Distinct by identity from every :class:`registry.Project`, so callers can
    detect it with ``is`` and render UNATTRIBUTED. A card whose workspace_path
    is empty, or resolves to no registered repo, is UNATTRIBUTED — it must be
    surfaced, never silently dropped and never guessed into a project.
    """


# Module-level singleton for cheap identity checks (``result is UNATTRIBUTED``).
UNATTRIBUTED = Unattributed()


def _resolve_path(path) -> str:
    """Normalise a filesystem path for comparison.

    Expands a leading ``~``, resolves symlinks (``realpath``), and strips
    trailing slashes, so ``~/dev/flightdeck``, ``/Users/desac/dev/flightdeck/``
    and a symlinked checkout all reduce to the same canonical string. Returns
    ``""`` for empty/None input. ``realpath`` also normalises a path that does
    not exist on disk (it collapses ``.``/``..`` and ``//`` segments), which is
    what keeps tests that use synthetic paths deterministic.
    """
    if not path:
        return ""
    expanded = os.path.expanduser(str(path))
    try:
        resolved = os.path.realpath(expanded)
    except OSError:  # pragma: no cover - defensive
        resolved = expanded
    normalized = os.path.normpath(resolved)
    return normalized.rstrip("/") or normalized


def _workspace_belongs_to_repo(workspace: str, repo: str) -> bool:
    """True when the normalised ``workspace`` path is ``repo`` or under it.

    ``workspace == repo`` (the card's checkout is the repo root) and ``repo``
    is a path-prefix of ``workspace`` (a worktree at ``<repo>/.worktrees/...``)
    both mean the card's work lives in that project. Prefix matching operates on
    path segments (``repo + os.sep``) so a sibling like ``/dev/hscc-evil`` never
    matches ``/dev/hscc``.
    """
    if not workspace or not repo:
        return False
    if workspace == repo:
        return True
    return workspace.startswith(repo + os.sep)


def project_for_card(card: dict, projects: list) -> Any:
    """The project a card belongs to, resolved by its ``workspace_path``.

    Returns one of ``projects`` when the card's workspace_path is that project's
    repo or lives under it (both resolved + normalised), else the
    :data:`UNATTRIBUTED` sentinel. ``UNATTRIBUTED`` is returned for an empty /
    NULL workspace_path AND for a path matching no registered repo — never a
    guess into a project, never a silent drop.

    ``projects`` may be any iterable of objects exposing a ``repo`` attribute
    (typically ``registry.Project``); the return type is annotated ``Any`` so
    this module stays decoupled from the registry.
    """
    workspace = _resolve_path(card.get("workspace_path"))
    if not workspace:
        return UNATTRIBUTED
    for proj in projects:
        repo = _resolve_path(getattr(proj, "repo", None))
        if repo and _workspace_belongs_to_repo(workspace, repo):
            return proj
    return UNATTRIBUTED


def _main_tree_violation(card: dict, projects: list) -> Optional[dict]:
    """The project whose MAIN TREE ``card`` would run in, or None.

    A card actually operates in a registered project's MAIN (operator)
    checkout when its resolved ``workspace_path`` is the repo ROOT itself —
    the worker edits files directly in the main tree, fighting the operator,
    instead of in an isolated worktree.

    NARROWED (N17): an UNCLAIMED card at the repo root is NORMAL, not a
    defect. Hermes creates a card with ``workspace_kind='worktree'`` and
    ``workspace_path=<repo root>``; only when a worker CLAIMS the card does it
    rewrite ``workspace_path`` to ``<repo>/.worktrees/<id>``. So a card in
    status ready/todo/blocked legitimately shows the repo root — it simply has
    not been claimed yet. We therefore flag CRITICAL at the repo root only
    when it is a genuine hazard:

      * a NON-``worktree`` kind card points at the root — that card really
        would run in place, at ANY status; or
      * a ``worktree``-kind card with a worker attached (status running or
        claimed).

    A worktree-kind card that claims a NON-root, NON-``.worktrees`` location
    under the repo (e.g. ``<repo>/subdir``) is still flagged — that is never a
    normal pre-claim state, so it stays a defect regardless of status.

    Reuses :func:`_resolve_path` and :func:`_workspace_belongs_to_repo` — the
    SAME normalisation and path comparison ``project_for_card`` uses — never a
    second path implementation. Returns the violation dict
    (``{id, title, path, project}``) or None when the card is a normal worktree
    card or unattributed to any registered repo.
    """
    workspace = _resolve_path(card.get("workspace_path"))
    if not workspace:
        return None
    for proj in projects:
        repo = _resolve_path(getattr(proj, "repo", None))
        if not repo or not _workspace_belongs_to_repo(workspace, repo):
            continue
        in_worktree = workspace.startswith(repo + os.sep + ".worktrees" + os.sep)
        kind = str(card.get("workspace_kind") or "")
        active = str(card.get("status") or "") in ACTIVE_STATUSES
        if workspace == repo:
            # The repo ROOT — the operator's main checkout itself. CRITICAL
            # only when a worker is attached to a worktree-kind card, or the
            # card is NOT a worktree kind at all (runs in place, any status).
            # Unclaimed worktree-kind at the root is NORMAL: report nothing.
            if kind != "worktree" or active:
                return _violation_dict(card, proj, workspace)
        elif kind == "worktree" and not in_worktree:
            # Claims to be a worktree but points at a NON-root, NON-.worktrees
            # location under this repo — genuinely malformed, never a normal
            # pre-claim state, flagged regardless of status.
            return _violation_dict(card, proj, workspace)
    return None


def _violation_dict(card: dict, proj: Any, workspace: str) -> dict:
    """The main-tree violation shape shared by lint-cards and standup."""
    return {
        "id": card.get("id"),
        "title": card.get("title") or "(untitled)",
        "path": workspace,
        "project": getattr(proj, "name", None),
    }


def main_tree_cards(cards: list, projects: list) -> list[dict]:
    """Every card that would actually run in a registered project's MAIN tree.

    A card whose resolved ``workspace_path`` is a project's repo ROOT (instead
    of a worktree under ``<repo>/.worktrees/``) would operate directly in the
    operator's main checkout — the defect behind the 2026-08-11 near-miss.

    NARROWED (N17): an unclaimed card at the repo root is NORMAL (Hermes
    rewrites ``workspace_path`` to ``.worktrees/<id>`` only on claim), so it is
    NOT reported. CRITICAL fires only when a card is actually running at the
    root — a worker attached to a worktree-kind card, or any non-worktree card
    pointing at the root (which runs in place at any status). A worktree-kind
    card at a non-root, non-``.worktrees`` location under the repo is also
    flagged. Reporting is the whole job: nothing here moves or re-points a
    card.

    Archived cards are excluded (already settled, not part of any dispatch) —
    matching how ``list_cards`` reads by default.

    Returns a list of violation dicts ``{id, title, path, project}`` ordered by
    card id, empty when nothing would run in a main tree.
    """
    flagged: list[dict] = []
    for card in cards:
        if str(card.get("status") or "") == "archived":
            continue
        v = _main_tree_violation(card, projects)
        if v is not None:
            flagged.append(v)
    flagged.sort(key=lambda v: str(v.get("id") or ""))
    return flagged


def current_board(_kdb=None) -> str:
    """Resolve Hermes' active/current board slug (READ only, never creates).

    Thin wrapper over ``kanban_db.get_current_board`` so reading the current
    board is owned here (one seam for all board access), like ``list_boards``
    and ``create_task``. This is the FALLBACK a card-creating command uses
    for a project whose registry entry has no ``board`` — reading it is
    always safe, but a command must never rely on it as the PRIMARY target a
    card lands on. ``_kdb`` is the injectable kanban library (a fake in tests).
    """
    kdb = _kdb if _kdb is not None else _load_kanban_db()
    return str(kdb.get_current_board())


def resolve_project_board(project, _kdb=None) -> tuple[str, bool]:
    """The board a project's cards must land on, plus whether we fell back.

    Returns ``(board_slug, used_fallback)``. When the project's registry
    entry carries a ``board``, that board is used and ``used_fallback`` is
    ``False`` — the project's OWN board, never Hermes' global current board.
    When the project has NO board, we fall back to Hermes' CURRENT board and
    return ``used_fallback=True`` so the caller can SAY SO — a silent
    fallback is exactly how cards end up on the wrong board (the R1-R7
    incident). The caller always reports ``board_slug`` so where a card
    landed is visible. This never mutates the current board; ``_kdb`` is the
    injectable kanban library (a fake in tests).
    """
    board = getattr(project, "board", None)
    if board:
        return str(board), False
    return current_board(_kdb=_kdb), True


def create_task(
    board: str,
    title: str,
    *,
    assignee: Optional[str] = None,
    body: Optional[str] = None,
    workspace_kind: Optional[str] = None,
    workspace_path: Optional[str] = None,
    _kdb=None,
) -> str:
    """Create a card on ``board``; returns the new card id.

    Routes through the same Hermes kanban library and connection path as
    :func:`list_cards` — one seam for all board access, never a shell-out. The
    new card defaults to ``running`` (ready for a worker to pick up), matching
    the dispatch intent. ``assignee`` and ``body`` are optional.

    ``workspace_kind`` / ``workspace_path`` (both optional) let a caller
    anchor the new card in a specific repo's worktree — e.g. ``migrate-card``
    re-homes a card into the target project's repo. When omitted they default
    to Hermes' own ``scratch`` defaults, exactly as before.

    ``_kdb`` is the injectable kanban library (a fake in tests) so creating a
    card never touches the live board during the test suite — the same seam
    :func:`create_board` exposes.
    """
    kdb = _kdb if _kdb is not None else _load_kanban_db()
    conn = kdb.connect(board=board)
    try:
        kwargs: dict = {
            "title": title,
            "body": body,
            "assignee": assignee,
            "board": board,
        }
        # Forward the workspace anchors only when a caller actually set them
        # (migrate-card re-homes a card into the target repo); otherwise the
        # underlying call is byte-identical to before, so existing callers see
        # no change.
        if workspace_kind is not None:
            kwargs["workspace_kind"] = workspace_kind
        if workspace_path is not None:
            kwargs["workspace_path"] = workspace_path
        task_id = kdb.create_task(conn, **kwargs)
    finally:
        conn.close()
    return str(task_id)


def add_card_comment(card_id: str, body: str, author: Optional[str] = None, *, _kdb=None) -> int:
    """Add a comment to ``card_id``; returns the new comment id (an int).

    Resolves the card's owning board via :func:`find_card` (which handles both
    live and archived boards — an archived hit carries ``_board_path``) then
    opens that board's DB and calls ``kanban_db.add_comment``. ``author`` is
    required by the backing call's signature but defaults to None here so a
    caller (or test) that doesn't know the author can let the reader fall back.
    ``_kdb`` is the injectable kanban library (a fake in tests).
    """
    card = find_card(card_id, _kdb=_kdb)
    if card is None:
        raise KeyError(f"card {card_id!r} not found")
    kdb = _kdb if _kdb is not None else _load_kanban_db()
    db_path = card.get("_board_path")
    conn = kdb.connect(db_path=db_path) if db_path else kdb.connect(board=card.get("board"))
    try:
        return kdb.add_comment(conn, card_id, author, body)
    finally:
        conn.close()


def block_card(card_id: str, reason: str, kind: Optional[str] = None, *, _kdb=None) -> bool:
    """Block ``card_id``; returns True when the block landed.

    Resolves the card via :func:`find_card`, opens its board DB, and calls
    ``kanban_db.block_task``. ``_kdb`` is the injectable kanban library (a fake
    in tests).
    """
    card = find_card(card_id, _kdb=_kdb)
    if card is None:
        raise KeyError(f"card {card_id!r} not found")
    kdb = _kdb if _kdb is not None else _load_kanban_db()
    db_path = card.get("_board_path")
    conn = kdb.connect(db_path=db_path) if db_path else kdb.connect(board=card.get("board"))
    try:
        return bool(kdb.block_task(conn, card_id, reason=reason, kind=kind))
    finally:
        conn.close()


def close_card(card_id: str, result: Optional[str] = None, *, _kdb=None) -> bool:
    """Complete/close ``card_id``; returns True when the close landed.

    Resolves the card via :func:`find_card`, opens its board DB, and calls
    ``kanban_db.complete_task``. ``_kdb`` is the injectable kanban library (a
    fake in tests).
    """
    card = find_card(card_id, _kdb=_kdb)
    if card is None:
        raise KeyError(f"card {card_id!r} not found")
    kdb = _kdb if _kdb is not None else _load_kanban_db()
    db_path = card.get("_board_path")
    conn = kdb.connect(db_path=db_path) if db_path else kdb.connect(board=card.get("board"))
    try:
        return bool(kdb.complete_task(conn, card_id, result=result))
    finally:
        conn.close()


def edit_card(card_id: str, *, assignee: Optional[str] = None, _kdb=None) -> bool:
    """Edit ``card_id`` (assignee only); returns True when the edit landed.

    NOTE ON EDITABLE FIELDS: kanban_db exposes NO backing mutation for a card's
    ``title`` or ``body`` — there is no ``update_title``/``update_body``
    function. Only a card's ``assignee`` is changeable here, via
    ``kanban_db.assign_task``. Attempting to edit title/body is therefore out of
    scope (not backed); the route documents this and only accepts ``assignee``.

    Resolves the card via :func:`find_card`, opens its board DB, and calls
    ``kanban_db.assign_task`` when ``assignee`` is provided. ``_kdb`` is the
    injectable kanban library (a fake in tests).
    """
    card = find_card(card_id, _kdb=_kdb)
    if card is None:
        raise KeyError(f"card {card_id!r} not found")
    kdb = _kdb if _kdb is not None else _load_kanban_db()
    db_path = card.get("_board_path")
    conn = kdb.connect(db_path=db_path) if db_path else kdb.connect(board=card.get("board"))
    try:
        if assignee is not None:
            return bool(kdb.assign_task(conn, card_id, assignee))
        return True
    finally:
        conn.close()


def classify(
    card: dict,
    git_facts: dict,
    *,
    stale_threshold_seconds: int = DEFAULT_STALE_THRESHOLD_SECONDS,
    dead_days: int = DEFAULT_DEAD_DAYS,
    now: Optional[int] = None,
) -> str:
    """Classify one card as one of ``needs_you | stale | running | landed | dead``.

    ``card`` is a flightdeck card dict (as produced by :func:`list_cards`).
    ``git_facts`` is a ``{card_id: {branch_exists, is_merged, commits_ahead}}``
    mapping, keyed by card id, computed by the caller via git_state.

    Precedence (highest first) — the order is what makes the signal true:

    1. ``landed``  — branch exists AND is merged into main. The work landed,
       so the card should be CLOSED. This always wins, including over
       ``needs_you``: a merged branch is never "needs you".
    2. ``needs_you`` — status is review/blocked AND the branch genuinely
       exists and is unmerged. Only here does the operator's eyes-on matter.
    3. ``dead``    — no branch, no commits, older than ``dead_days`` days.
       Unstarted and abandoned -> flag for archive.
    4. ``stale``   — in-flight (claimed/running) longer than
       ``stale_threshold_seconds`` AND zero commits ahead. A card that has
       been running for hours WITH commits is ``running``, not stale.
    5. ``running`` — everything else. In flight, nothing needed.

    This function never mutates ``card`` or ``git_facts``.
    """
    card_id = card.get("id")
    facts = _facts_for(git_facts, card_id)
    status = str(card.get("status") or "")

    # 1. Landed: work reached main -> the card should be closed.
    if facts["branch_exists"] and facts["is_merged"]:
        return "landed"

    # 2. Needs you: awaiting review AND genuinely unmerged.
    if status in REVIEW_STATUSES and facts["branch_exists"] and not facts["is_merged"]:
        return "needs_you"

    # 3. Dead: never started, no work, abandoned. The branch must not exist AND
    # carry no commits of its own (own_commits == 0) — a card whose branch
    # carries commits is NEVER archived, even if the branch later went away.
    dead_seconds = dead_days * 86400
    if (
        not facts["branch_exists"]
        and facts["commits_ahead"] == 0
        and facts["own_commits"] == 0
        and _older_than(card, dead_seconds, field="created_at", now=now)
    ):
        return "dead"

    # 4. Stale: claimed past the threshold with zero progress (commits).
    if (
        status in IN_FLIGHT_STATUSES
        and facts["commits_ahead"] == 0
        and _older_than(card, stale_threshold_seconds, field="started_at", now=now)
    ):
        return "stale"

    # 5. In flight, nothing needed.
    return "running"


def _close_ineligible_reason(
    card: dict,
    facts: dict,
    heartbeat_window_seconds: int,
    *,
    now: Optional[int] = None,
) -> Optional[str]:
    """Why a merged-branch card is NOT closeable, or None if it is.

    A card may be proposed for CLOSE only when ALL of these hold:
      1. its branch is an ancestor of main (checked by the caller), AND
      2. the branch actually LANDED (``landed_via_merge``) — being an ancestor
         of main is not enough, since an unstarted branch is one too, AND
      3. the card is not active — not in an active status and no recent heartbeat.

    Returns a human-readable reason string when the card must be skipped, else
    ``None`` (meaning it is eligible to close). This is the SEV-1 guard: a
    branch that merely looks merged because main advanced past its fork point,
    carrying zero commits of its own, must NEVER be closed.
    """
    if not facts.get("landed_via_merge", False):
        return "branch never landed (ancestor of main, but no merge carried it)"
    status = str(card.get("status") or "")
    if status in ACTIVE_STATUSES:
        return f"card is active (status={status})"
    if _recent(card, heartbeat_window_seconds, field="last_heartbeat_at", now=now):
        return "card heartbeat within the active window"
    return None


def reconcile_plan(
    cards: list[dict],
    git_facts: dict,
    *,
    stale_threshold_seconds: int = DEFAULT_STALE_THRESHOLD_SECONDS,
    dead_days: int = DEFAULT_DEAD_DAYS,
    heartbeat_window_seconds: int = DEFAULT_HEARTBEAT_WINDOW_SECONDS,
    now: Optional[int] = None,
) -> dict:
    """Build the reconciliation plan.

    Returns ``{close: [...], archive: [...], stale: [...], skipped: [...]}``.
    A PLAN only — this function never mutates the board. ``close``, ``archive``
    and ``stale`` are lists of card ids; ``skipped`` is a list of
    ``{"id": ..., "reason": ...}`` dicts.

    * ``close``   — branch merged into main AND carries work AND the card is
                    not active. Auto-close is the missing transition that fixed
                    the phantom backlog, so these are the primary intent.
    * ``archive`` — classified ``dead`` (unstarted + abandoned). Dead explicitly
                    requires the branch to carry no commits, so a card whose
                    branch carries work can never be archived.
    * ``stale``   — classified ``stale`` (in flight, zero commits past the
                    threshold).
    * ``skipped`` — a card whose branch looks merged but which fails a
                    close-eligibility gate (no own commits / active / recent
                    heartbeat), reported with the reason so it is visible
                    rather than silently dropped.

    ``needs_you`` and ``running`` cards are deliberately absent from the plan:
    needs-you is reported in standup (not auto-mutated), and running needs
    nothing. The reconcile command acts on the ids here and nothing else.
    """
    plan: dict[str, list] = {"close": [], "archive": [], "stale": [], "skipped": []}
    for card in cards:
        card_id = card.get("id")
        if card_id is None:
            # A card with no id cannot be reconciled against — skip it rather
            # than injecting a None into the plan.
            continue
        facts = _facts_for(git_facts, card_id)

        # Landed branch: gate it through the close-eligibility checks. A branch
        # that is an ancestor of main but carries NO work of its own (or an
        # active card) goes to SKIPPED, never to close.
        if facts["branch_exists"] and facts["is_merged"]:
            reason = _close_ineligible_reason(
                card,
                facts,
                heartbeat_window_seconds,
                now=now,
            )
            if reason is None:
                plan["close"].append(card_id)
            else:
                plan["skipped"].append({"id": card_id, "reason": reason})
            continue

        label = classify(
            card,
            git_facts,
            stale_threshold_seconds=stale_threshold_seconds,
            dead_days=dead_days,
            now=now,
        )
        if label == "dead":
            plan["archive"].append(card_id)
        elif label == "stale":
            plan["stale"].append(card_id)
    return plan


# --------------------------------------------------------------------------- #
# Fleet-wide concurrency — the per-board cap is enforced per board, so the
# fleet's real pressure is the SUM across every board. Creating a board per
# project multiplies effective concurrency; these helpers surface that sum
# (and the config cap) so ``start`` and ``standup`` can make it visible and
# refuse to oversubscribe the shared worker span.
# --------------------------------------------------------------------------- #

# Hermes' own config, where the fleet concurrency knob lives. Overridable
# via env for tests / unusual installs (mirrors start.py's overridable seam).
_DEFAULT_CONFIG_PATH = os.path.expanduser("~/.hermes/config.yaml")

# Super-safe fallback: never assume more capacity than we can verify.
_DEFAULT_MAX_IN_PROGRESS = 1


def read_max_in_progress(path: Optional[str] = None) -> int:
    """The fleet concurrency cap (``kanban.max_in_progress``) from Hermes config.

    Reads the ``kanban.max_in_progress`` key of the Hermes config yaml
    (``path`` injectable for tests). Falls back to ``_DEFAULT_MAX_IN_PROGRESS``
    (1) when the file/key is missing or unparseable — the conservative
    reading, never a higher cap than the config actually declares. Both
    ``start`` and ``standup`` read the cap from here so it is READ, not
    hardcoded, and defined in one place.
    """
    p = path if path is not None else _DEFAULT_CONFIG_PATH
    try:
        with open(p, encoding="utf-8") as f:
            doc = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return _DEFAULT_MAX_IN_PROGRESS
    if not isinstance(doc, dict):
        return _DEFAULT_MAX_IN_PROGRESS
    section = doc.get("kanban")
    if not isinstance(section, dict):
        return _DEFAULT_MAX_IN_PROGRESS
    val = section.get("max_in_progress")
    if val is None:
        return _DEFAULT_MAX_IN_PROGRESS
    try:
        return int(val)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_IN_PROGRESS


def in_flight_by_board(cards: list[dict]) -> dict[str, int]:
    """Group ACTIVE (in-flight) cards by board across the fleet.

    ``cards`` is any iterable of flightdeck card dicts (as produced by
    :func:`list_cards`). Returns ``{board_slug: count}`` for every board with
    at least one in-flight card (status ``running`` or ``claimed`` — a worker
    is engaged). A review/blocked/todo card is NOT in flight: no worker is on
    it. A card with no board is excluded (the footer names boards, so there is
    nothing to name). Order follows ``cards`` input order.
    """
    counts: dict[str, int] = {}
    for card in cards:
        if str(card.get("status") or "") not in ACTIVE_STATUSES:
            continue
        board = card.get("board")
        if not board:
            continue
        board = str(board)
        counts[board] = counts.get(board, 0) + 1
    return counts


def fleet_in_flight() -> dict[str, int]:
    """In-flight cards per board across EVERY board on the host.

    Reads every board (:func:`list_cards` with ``board=None``) and groups the
    in-flight cards by board — the FLEET-WIDE reading. Hermes enforces
    ``kanban.max_in_progress`` per board, so with a board per project the
    effective concurrency multiplies; summing across all boards is the only
    true measure of pressure on the shared worker span.
    """
    return in_flight_by_board(list_cards())
