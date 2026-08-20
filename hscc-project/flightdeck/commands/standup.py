"""standup.py — `flightdeck standup` : the daily digest, one-shot or --watch.

The digest is a single reusable report. The same renderer is used by the
one-shot command and by every frame of ``--watch`` — they CANNOT drift, because
both go through :func:`render_digest`. Data is gathered by a single call,
:func:`gather_data`, so the ``standup`` card (or any other caller) can supply
the exact same ``data`` dict the renderer knows how to draw.

Watch mode adds only the presentation shell around that renderer: it clears
the screen between frames, prints a header carrying the timestamp and the
refresh interval, and handles ``Ctrl-C`` by exiting 0. If a single refresh
fails (a transient kanban lock, a flaky git read) it keeps the last good frame,
prints one error line under it, and keeps looping — an unattended monitor must
not die on one transient error.

All external access is injectable: ``_run`` (git subprocess), ``_now`` (clock)
and ``_sleep`` (the loop's pause) so tests never touch git, a real board, or
real time. The clock and sleep are injected separately so the watch loop can be
driven with an advancing fake clock while the suite stays fast.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Callable, Iterator, Optional

import yaml

from ..core import deployment, git_state, kanban, registry, verify
from . import legacy  # reuse legacy-cards' board-attribution rule (orphan boards)

DEFAULT_INTERVAL = 30  # seconds between watch frames

# Section order, most interruptive first (per DESIGN).
_SECTIONS = (
    ("NEEDS YOU", "needs_you"),
    ("FAILING", "failing"),
    ("STALE", "stale"),
    ("RUNNING", "running"),
    ("DRIFT", "drift"),
    ("UNREADABLE", "unreadable"),
)


# --------------------------------------------------------------------------- #
# data gathering — the single call the renderer consumes
# --------------------------------------------------------------------------- #

def gather_data(
    registry_path: str,
    *,
    _run: Optional[Callable] = None,
    _now: Optional[Callable] = None,
    _state: Optional[str] = None,
    _listdir: Optional[Callable] = None,
    _config: Optional[str] = None,
    _ceiling: int = 3,
    _update_state: Optional[str] = None,
    _watermarks: Optional[dict] = None,
) -> dict:
    """Read the registry and assemble the digest data in one call.

    Walks each registered project that declares a board, reads its cards, and
    classifies them against git facts (branch exists / merged / commits ahead)
    into the report's work-state buckets:

    * ``needs_you`` — awaiting review (review/blocked) with a genuinely
      unmerged branch.
    * ``failing``   — project whose last ``verify`` run (recorded in the state
      file) FAILED.
    * ``stale``     — claimed/running past the threshold with zero commits.
      Each row carries a ``kind``: ``starved`` when its worktree is empty (an
      infrastructure problem — the worker was handed nothing), plain ``stale``
      otherwise.
    * ``running``   — in flight, nothing needed. A card with files present but
      no commit is *working*, so it lands here, never in STALE.
    * ``drift``     — one row per registered project comparing the repo
      version against the installed one (OK / DRIFTED / UNKNOWN).
    * ``unreadable`` — one row per project whose repo path does not exist on
      disk, or whose board DB could not be opened. These projects are surfaced
      loudly with the reason — never reduced to a clean row.

    Every read goes through the same injectable seam (``kanban.list_cards`` via
    monkeypatch in tests, ``git_state`` through ``_run``, the state file via
    ``_state``, worktree contents via ``_listdir``) so a test never touches a
    real board or repo. A card with no board-mapped repo is assigned the
    conservative no-branch reading, never claimed merged.

    Cards are gathered from every board in ONE pass, then ATTRIBUTED to a
    project by their ``workspace_path`` (via ``kanban.project_for_card``) — the
    shared ``default`` board holds cards from many projects, so the board slug
    cannot tell which project a card belongs to. A card whose workspace_path
    resolves to no registered project's repo is surfaced as UNATTRIBUTED under
    ``running``, never silently dropped and never guessed into a project.

    Besides the per-section rows, the returned dict carries a ``coverage``
    sub-dict stating what was actually READ — project / board / card /
    attributed / unreadable counts plus the unreadable and no-source project
    lists — so a blind digest is always unmistakable.

    Returns ``{section_key: [row_dict, ...], coverage: {...}}``. Rows are
    ordered deterministically (board then card id for cards, project name for
    the per-project sections).
    """
    projects = registry.load_registry(registry_path)

    data: dict[str, Any] = {section_key: [] for _, section_key in _SECTIONS}

    coverage: dict[str, Any] = {
        "projects": len(projects),
        "boards": 0,
        "cards": 0,
        "attributed": 0,
        "unreadable": 0,
        "unreadable_projects": [],
        "no_source": [],
        # Fleet-wide concurrency: the per-board cap is enforced per board, so
        # the real pressure on the shared workers is the SUM across every
        # board. Read the cap from config (never hardcoded); the ceiling is
        # the --max-fleet warning threshold.
        "cap": kanban.read_max_in_progress(_config),
        "ceiling": int(_ceiling),
        "in_flight": 0,
        "in_flight_boards": [],
        # Freshness watermark: the newest state-change each contributing board
        # recorded, aggregated to the OLDEST critical input (see
        # kanban.freshness_watermark). ``watermark`` is that epoch timestamp
        # (None -> unknown); ``watermark_boards`` names the boards whose
        # freshness the watermark reflects, so an old watermark is attributable.
        "watermark": None,
        "watermark_boards": [],
        # Cross-project dependents: one entry per project that OTHER registered
        # projects declare (via ``depends_on``). Advisory only — a nudge that
        # an edit to that project carries downstream risk, never a gate.
        "dependents": [],
    }
    boards: set[str] = set()

    # Cross-project dependents: every project other registered projects depend
    # on, computed purely from the registry (no board/git/IO). Advisory only.
    coverage["dependents"] = _dependents_coverage(projects)

    # Projects whose repo cannot be read are reported, not silently skipped.
    readable = []
    for project in projects:
        reason = _unreadable_reason(project)
        if reason:
            _mark_unreadable(project, reason, coverage, data)
        else:
            readable.append(project)

    # ONE pass over every board; the board slug is not the attribution rule.
    try:
        cards = kanban.list_cards()
    except kanban.KanbanError as exc:
        # The single read failed, so NOTHING is known about any project's cards.
        # Say so against every project rather than rendering a clean digest.
        for project in list(readable):
            _mark_unreadable(project, f"could not read cards: {exc}", coverage, data)
        readable = []
        cards = []
    coverage["cards"] = len(cards)
    # Reuse legacy-cards' board-attribution rule (which boards the registry
    # maps) so the two commands can never disagree about what counts as an
    # orphan board. The shared `default` board is a legitimate, always-present
    # board whose cards ARE surfaced by workspace attribution — it is NOT a
    # hidden orphan. The footer only flags a board when it holds a card that
    # standup does NOT otherwise surface: on an unregistered board AND
    # unattributed (workspace resolves to no project). Those are the genuinely
    # hidden second-source-of-truth cards.
    registered_boards = legacy._registered_boards(projects)
    hidden: dict[str, int] = {}  # orphan board slug -> count of hidden cards
    attributed_projects: set[str] = set()
    for card in cards:
        project = kanban.project_for_card(card, readable)
        # project_for_card returns the UNATTRIBUTED sentinel, not None, when a
        # card's workspace_path matches no registered repo.
        if project is not kanban.UNATTRIBUTED and getattr(project, "name", None):
            coverage["attributed"] += 1
            attributed_projects.add(project.name)
        # Counts the boards that actually contributed cards in this pass, not
        # the boards projects declare -- the footer states what was READ.
        board = card.get("board")
        if board:
            boards.add(str(board))
            # A card NOT otherwise surfacing: on a board the registry doesn't
            # own, AND unattributed (no workspace resolves to a project). The
            # shared `default` board's cards resolve by workspace, so they are
            # surfaced and never counted here.
            if (
                str(board) not in registered_boards
                and project is kanban.UNATTRIBUTED
            ):
                hidden[str(board)] = hidden.get(str(board), 0) + 1
        row = _classify_row(card, project, _run=_run, _listdir=_listdir)
        bucket = row.pop("_bucket")
        data[bucket].append(row)

    # A project with no board mapping AND no attributed cards has no card
    # source at all — say so rather than rendering it as a clean project.
    for project in readable:
        if not project.board and project.name not in attributed_projects:
            coverage["no_source"].append(project.name)

    coverage["boards"] = len(boards)
    coverage["unread_boards"] = sorted(hidden.keys())
    coverage["unread_cards"] = sum(hidden.values())
    # Version-drift notice for flightdeck's OWN version vs its remote (the
    # "merged is not live" tripwire). Rate-limited to once/day: the daily
    # ls-remote only fires when the cache is stale; within a long --watch
    # session every frame after the first reads the cached verdict, so the
    # network is never hammered on a 30s redraw loop.
    coverage["update"] = _version_update_notice(
        projects, _run=_run, _now=_now, _update_state=_update_state
    )
    # Freshness watermark for the digest. ``boards`` is the set of boards that
    # contributed cards this pass — exactly the "critical inputs" behind the
    # digest. The watermark is the OLDEST freshness among them, so a stale
    # board can never hide behind a fresher one. An injectable ``_watermarks``
    # map (tests) or a fresh read of every contributing board feeds it; an
    # empty/quiet board that contributed no card never lowers the watermark.
    if boards:
        wm_boards = sorted(boards)
        watermarks = _watermarks
        if watermarks is None:
            watermarks = kanban.list_board_watermarks(boards=wm_boards)
        watermark = kanban.freshness_watermark(
            wm_boards, watermarks=watermarks or {}
        )
        if watermark is not None:
            coverage["watermark"] = watermark
            coverage["watermark_boards"] = [
                b for b in wm_boards if (watermarks or {}).get(b) is not None
            ]
    # Main-tree cards: any non-archived card that would run in a project's
    # MAIN TREE (repo root, or a worktree-kind card not under .worktrees/).
    # Surfaced as a distinct, most-interruptive line — never buried among
    # advisory lint output.
    main_tree = kanban.main_tree_cards(cards, readable)
    data["main_tree"] = main_tree
    coverage["main_tree"] = len(main_tree)
    # Fleet-wide in-flight: group the ACTIVE cards (running/claimed) just read
    # by their board across the WHOLE fleet. Hermes enforces the cap per
    # board, so summing across every board is the only true measure of
    # pressure on the shared worker span — this is the six-boards-one-cap
    # failure made visible.
    in_flight = kanban.in_flight_by_board(cards)
    coverage["in_flight"] = sum(in_flight.values())
    coverage["in_flight_boards"] = sorted(in_flight.keys())
    data["coverage"] = coverage

    data["failing"] = _failing_rows(projects, _state=_state)
    data["drift"] = _drift_rows(projects, _run=_run)

    # board is None for a card attributed by workspace_path with no board
    # mapping, and None is not orderable against str -- sort those last rather
    # than crashing the whole digest.
    for bucket in ("needs_you", "stale", "running"):
        data[bucket].sort(key=lambda r: (r.get("board") or "\uffff", r.get("id") or ""))
    for bucket in ("failing", "drift", "unreadable"):
        data[bucket].sort(key=lambda r: r.get("project") or "")
    return data


def _unreadable_reason(project: registry.Project) -> Optional[str]:
    """Why a project cannot be read, or None if it can.

    A project's repo path must exist on disk for its cards and git facts to be
    readable. A missing repo path is the unreadable case — surfaced loudly
    rather than reduced to a clean row. ``_isdir`` (module-level, monkeypatchable
    in tests) is used so no test touches the filesystem.
    """
    isdir = _isdir or os.path.isdir
    if not isdir(project.repo):
        return f"repo path does not exist: {project.repo}"
    return None


def _mark_unreadable(
    project: registry.Project,
    reason: str,
    coverage: dict,
    data: dict,
) -> None:
    """Record ``project`` as unreadable in both the coverage counts and rows.

    ``coverage`` carries the aggregate footer counts; ``data["unreadable"]``
    carries the per-project rows the renderer draws (the reason included, so
    the listing is never a bare name).
    """
    coverage["unreadable"] += 1
    coverage["unreadable_projects"].append(
        {"project": project.name, "reason": reason}
    )
    data["unreadable"].append(
        {"project": project.name, "board": project.board, "reason": reason}
    )


def _failing_rows(projects: list, *, _state: Optional[str] = None) -> list[dict]:
    """Projects whose last recorded ``verify`` run FAILED.

    Reads the state written by the ``verify`` command (:func:`verify.load_state`).
    A project appears in FAILING only when its latest record's status is FAIL —
    the digest reports what was last verified, never re-running verify (that is
    ``verify``'s job, and a re-run on every watch frame would be wasteful). A
    project with no record, or one whose last run passed or was skipped, is not
    failing: absence here is the honest "nothing currently failing" reading.
    """
    state = verify.load_state(_state)
    verify_map = state.get("verify", {}) if isinstance(state, dict) else {}
    rows: list[dict] = []
    for project in projects:
        record = verify_map.get(project.name)
        if not isinstance(record, dict):
            continue
        if record.get("status") != verify.FAIL:
            continue
        rows.append(
            {
                "project": project.name,
                "board": project.board,
                "status": verify.FAIL,
                "timestamp": record.get("timestamp"),
                "duration_s": record.get("duration_s"),
            }
        )
    return rows


def _drift_rows(projects: list, *, _run: Optional[Callable] = None) -> list[dict]:
    """One DRIFT row per registered project (repo version vs installed).

    Uses :func:`deployment.version_drift`, which returns the three-state
    (OK / DRIFTED / UNKNOWN) verdict. UNKNOWN is deliberately distinct from OK:
    a project we could not check (no installed_version_cmd, a failing command,
    a missing version file) is *unknown*, never reported as OK. Every project
    appears — including one with no board — with its board marked ``unknown``
    when absent, because a silently omitted project is the failure this tool
    exists to prevent.
    """
    rows: list[dict] = []
    for project in projects:
        repo_version, installed, state = deployment.version_drift(project, _run=_run)
        rows.append(
            {
                "project": project.name,
                "board": project.board,  # None -> rendered as "unknown"
                "repo_version": repo_version,
                "installed": installed,
                "state": state,
            }
        )
    return rows


# --------------------------------------------------------------------------- #
# version-drift notice — flightdeck's OWN version vs its remote (rate-limited)
# --------------------------------------------------------------------------- #
#
# The DRIFT section above compares each project's repo VERSION against its
# installed version. This is a DIFFERENT, narrower check: flightdeck's OWN
# installed version against the newest tag on its OWN git remote — the
# "merged is not live" tripwire (a fix merged upstream that the running copy
# doesn't have). It is a pure NOTICE: never auto-applies anything, and the
# remote call is rate-limited to once per day via a cache file under the same
# ~/.flightdeck convention as qa-notified.yaml, so a long-running `--watch`
# session never re-fetches every 30s.

# The cache file name (under ~/.flightdeck), and how long a cached verdict is
# trusted before the next ls-remote fires. Mirrors the qa-notified.yaml pattern
# the task names. HERMES_HOME redirects the default into a test sandbox.
UPDATE_CACHE_DEFAULT = "update-check.yaml"
UPDATE_CACHE_TTL_SECONDS = 86400  # once per day
_UPDATE_HOME_ENV = "HERMES_HOME"
_FLIGHTDECK_PROJECT = "flightdeck"


def _update_home() -> str:
    """The default root for the update-check cache (honours HERMES_HOME)."""
    root = os.environ.get(_UPDATE_HOME_ENV) or "~/.flightdeck"
    return os.path.expanduser(root)


def _update_state_path(_update_state: Optional[str] = None) -> str:
    """Where the update-check cache lives, or an injected override (tests)."""
    if _update_state:
        return os.path.expanduser(str(_update_state))
    return os.path.join(_update_home(), UPDATE_CACHE_DEFAULT)


def _version_tuple(v) -> tuple:
    """``'0.6.0'`` / ``'v0.6.0'`` -> ``(0, 6, 0)`` for ordering.

    Extracts the leading run of integers from each dot-separated component, so
    a ``v`` prefix and any non-numeric suffix are ignored. An unparseable
    version degrades to ``(0,)`` — still orderable, never an error.
    """
    import re

    return tuple(int(p) for p in re.findall(r"\d+", str(v))) or (0,)


def _latest_tag(ls_remote_out: str) -> Optional[str]:
    """The newest ``refs/tags/vX.Y.Z`` ref, or None when none exist.

    Skips the peeled ``^{}`` refs (Hermes-style tags emit both the tag object
    and the peeled commit), and picks the max by :func:`_version_tuple` so the
    answer never depends on ``ls-remote``'s own sort order. A remote with no
    version tags yields None -> no notice (UNKNOWN), never a false positive.
    """
    best: Optional[str] = None
    best_t: tuple = (0,)
    for line in ls_remote_out.splitlines():
        if "refs/tags/v" not in line or line.endswith("^{}"):
            continue
        tag = line.split("refs/tags/")[-1].strip()
        t = _version_tuple(tag)
        if t > best_t:
            best, best_t = tag, t
    return best


def _installed_flightdeck_version(project, *, _run: Optional[Callable] = None) -> Optional[str]:
    """flightdeck's OWN installed version via its registry ``installed_version_cmd``.

    Reuses deployment's injectable runner, so a test never executes a real
    command. None when the project declares no such command or it fails — the
    honest UNKNOWN, never folded into a value.
    """
    cmd = getattr(project, "installed_version_cmd", None)
    if not cmd:
        return None
    cp = deployment._dispatch(cmd, project.repo, _run)
    if cp.returncode != 0:
        return None
    return (cp.stdout or "").strip() or None


def _fresh_update_check(project, *, _run: Optional[Callable] = None):
    """``(remote_latest_tag, installed_version)`` via one cheap ls-remote.

    The ONLY network-reaching call in the whole module. ``git ls-remote``
    touches the remote but never fetches or mutates anything locally. A
    non-zero exit (offline, no origin) degrades to ``(None, installed)`` -> no
    notice, never a fabricated one.
    """
    installed = _installed_flightdeck_version(project, _run=_run)
    cp = git_state._dispatch(
        ["git", "ls-remote", "--tags", "--sort=-v:refname", "origin", "v*"],
        project.repo,
        _run,
    )
    if cp.returncode != 0:
        return (None, installed)
    return (_latest_tag(cp.stdout or ""), installed)


def _load_update_cache(path: str) -> Optional[dict]:
    """The cached verdict, or None when absent/unreadable/ill-formed.

    An absent file, an unparseable file, or one missing a valid ``checked_at``
    are all None -> the next call does a fresh check. A malformed cache never
    raises and never fabricates a verdict.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    if not isinstance(data.get("checked_at"), (int, float)):
        return None
    return data


def _save_update_cache(path: str, data: dict) -> bool:
    """Persist the verdict. Failure is silent (a notice is never worth a crash)."""
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh, sort_keys=False)
        return True
    except OSError:
        return False


def _version_update_notice(
    projects: list,
    *,
    _run: Optional[Callable] = None,
    _now: Optional[Callable] = None,
    _update_state: Optional[str] = None,
) -> Optional[str]:
    """The ``flightdeck update available`` line, or None when none.

    Compares flightdeck's OWN installed version against the newest tag on its
    own git remote. Rate-limited to once per day: the ls-remote only fires
    when the cache is stale (``checked_at`` older than
    :data:`UPDATE_CACHE_TTL_SECONDS`); every other call reads the cached
    verdict, so a long-running ``--watch`` session never re-hits the network
    on every 30s redraw. Never auto-applies anything — purely a notice pointing
    at ``flightdeck update``.

    Returns None when there is no registered ``flightdeck`` project, neither
    version is determinable (UNKNOWN), or the installed version is already the
    newest. A None return prints nothing extra in the footer.
    """
    project = next((p for p in projects if p.name == _FLIGHTDECK_PROJECT), None)
    if project is None or not getattr(project, "repo", None):
        return None  # no self-entry in the registry -> nothing to compare
    if not getattr(project, "installed_version_cmd", None):
        # No way to know what's actually running -> no comparison is possible,
        # so there is no reason to hit the remote at all. UNKNOWN, not a notice.
        return None

    now = _now() if _now is not None else time.time()
    path = _update_state_path(_update_state)

    cached = _load_update_cache(path)
    if cached is not None and now - cached["checked_at"] < UPDATE_CACHE_TTL_SECONDS:
        latest, installed = cached.get("latest"), cached.get("installed")
    else:
        latest, installed = _fresh_update_check(project, _run=_run)
        _save_update_cache(
            path, {"checked_at": now, "latest": latest, "installed": installed}
        )

    if not latest or not installed:
        return None  # either side unknown -> no notice (UNKNOWN)
    if _version_tuple(latest) <= _version_tuple(installed):
        return None  # installed is current or newer
    return (
        f"flightdeck update available: installed {installed}, "
        f"remote {latest} (run flightdeck update)"
    )


def _classify_row(
    card: dict,
    project: Any,
    *,
    _run: Optional[Callable],
    _listdir: Optional[Callable] = None,
) -> dict:
    """One card's digest row, bucketed by kanban.classify against git facts.

    Returns a dict augmented with ``_bucket`` (popped by the caller) carrying
    the target section key.

    ``project`` is the result of ``kanban.project_for_card``: a real
    :class:`registry.Project` when the card's workspace_path matched a project's
    repo, or ``kanban.UNATTRIBUTED`` when it could not be placed. An
    UNATTRIBUTED card has no repo to check git facts against, so it is given the
    conservative ``running`` reading and surfaced distinctly (flagged
    ``unattributed=True``) — it is shown, never dropped and never guessed into a
    project whose repo it does not live under.

    A card on a board with no mapped repo (or a card with no id) is subject to
    the same conservative ``running`` reading: it is shown, not dropped, per
    "never silently omit a project/card".

    A card that classifies ``stale`` (claimed past the threshold, zero commits)
    is refined by looking at its worktree: an EMPTY worktree means the worker is
    STARVED — an infrastructure problem, surfaced distinctly under STALE — while
    files-present-but-no-commit means the worker is genuinely WORKING, so the
    card moves out of STALE entirely into RUNNING. ``kind`` carries
    ``starved``/``working`` on STALE rows so the renderer distinguishes them.
    """
    if project is kanban.UNATTRIBUTED or project is None:
        # Cannot classify against a repo: conservative in-flight reading,
        # surfaced as UNATTRIBUTED so the card is never silently dropped.
        return {
            "_bucket": "running",
            "id": card.get("id"),
            "board": None,
            "title": card.get("title") or "untitled",
            "status": str(card.get("status") or ""),
            "branch": card.get("branch"),
            "assignee": card.get("assignee"),
            "verify": None,
            "kind": "working",
            "unattributed": True,
        }

    card_id = card.get("id")
    branch = card.get("branch") or (f"wt/{card_id}" if card_id else None)

    if project.repo and card_id and branch:
        exists = git_state.branch_exists(project.repo, branch, _run=_run)
        facts = {
            "branch_exists": exists,
            "is_merged": git_state.is_merged(project.repo, branch, _run=_run) if exists else False,
            "commits_ahead": git_state.commits_ahead(project.repo, branch, _run=_run) if exists else 0,
        }
        # classify expects git_facts keyed by card_id and looks the card up
        # internally via _facts_for(git_facts, card.get("id")); we key the
        # single card's facts by its id so the real classification runs.
        label = kanban.classify(card, {card_id: facts})
    else:
        label = "running"  # cannot verify -> in-flight reading, never "landed"

    bucket = "running"
    kind = "working"  # the default non-stale reading
    if label == "needs_you":
        bucket = "needs_you"
    elif label == "stale":
        # Refine stale: an empty worktree is starvation; files present with no
        # commit is working (moved out of STALE into RUNNING).
        if _worktree_empty(project, card_id, _listdir=_listdir):
            bucket, kind = "stale", "starved"
        else:
            bucket, kind = "running", "working"

    # Needs-you rows carry the registry verify command so the renderer can show
    # the VERIFY: line on exactly the cards that need eyes-on.
    return {
        "_bucket": bucket,
        "id": card_id,
        "board": str(board) if (board := card.get("board")) else str(project.board),
        "title": card.get("title") or "untitled",
        "status": str(card.get("status") or ""),
        "branch": branch,
        "assignee": card.get("assignee"),
        "verify": project.verify,
        "kind": kind,
    }


def _worktree_empty(
    project: registry.Project,
    card_id,
    *,
    _listdir: Optional[Callable] = None,
) -> bool:
    """True when the card's worktree checkout is empty (or absent) -> starved.

    The worktree lives at ``<repo>/.worktrees/<card_id>``. Empty means the
    worker claimed the card but the infrastructure handed it no files to edit —
    starvation, an environment/executor problem rather than the worker stalling.
    A missing directory and a directory with no visible files are both the
    "handed nothing" reading (return True). ``_listdir`` returns the directory
    entries and is injectable so tests never touch the filesystem.
    """
    if not project.repo or not card_id:
        return True  # cannot inspect -> assume no work was provisioned
    root = os.path.join(project.repo, ".worktrees", str(card_id))
    isdir = _isdir or os.path.isdir
    if not isdir(root):
        return True
    listdir = _listdir if _listdir is not None else os.listdir
    try:
        return all(e.startswith(".") for e in listdir(root))
    except OSError:
        return True  # unreadable -> treat as empty, never claim working


def _isdir(path: str) -> bool:
    """Production directory check; monkeypatched in tests (see _worktree_empty)."""
    return os.path.isdir(path)


# --------------------------------------------------------------------------- #
# renderer — the single source of truth for digest output
# --------------------------------------------------------------------------- #

def render_digest(data: dict) -> str:
    """Render the digest body for ``data``.

    Deterministic given ``data``: identical bytes whether produced by the
    one-shot command or by a single watch frame, because both call this same
    function. Sections are drawn in the DESIGN's interruptiveness order; a
    section with no rows shows ``none``. The timestamp / interval header is
    deliberately NOT here — it is the watch shell's concern, so one-shot output
    and the digest body of every frame are byte-identical.

    A COVERAGE footer is ALWAYS appended (never behind a flag). It states what
    the digest was actually able to read, so a blind digest is impossible to
    mistake for a clean one. ``data`` may omit ``coverage`` (e.g. a synthetic
    watch frame) — the renderer falls back to a zero reading without error.

    A MAIN TREE block is prepended (before every section) whenever any card
    would run in a registered project's main tree. It is more interruptive
    than a stale card and must never be buried among advisory output.
    """
    lines: list[str] = []
    lines.extend(_render_main_tree(data))
    for name, key in _SECTIONS:
        rows = data.get(key, [])
        lines.append(f"{name} ({len(rows)})")
        if not rows:
            lines.append("  none")
        for row in rows:
            lines.append(_render_row(name, row))
        lines.append("")
    # strip the trailing blank line added after the last section
    if lines and lines[-1] == "":
        lines.pop()
    lines.extend(_render_coverage(_coverage_for(data)))
    return "\n".join(lines)


def _render_main_tree(data: dict) -> list[str]:
    """The most-interruptive MAIN TREE block, or nothing when all clear.

    A card whose resolved workspace_path is a registered project's repo ROOT
    (or a worktree-kind card not under ``<repo>/.worktrees/``) would run
    directly in the operator's main checkout — the defect behind the
    2026-08-11 near-miss. Each offending card is named with its id, title and
    workspace path. When nothing would run in a main tree, returns an empty
    list so a clean digest renders exactly as before.
    """
    main_tree = data.get("main_tree") or []
    if not main_tree:
        return []
    word = "card" if len(main_tree) == 1 else "cards"
    lines: list[str] = ["MAIN TREE"]
    lines.append(
        f"!!! CRITICAL: {len(main_tree)} {word} would run in a repo's MAIN TREE "
        f"— fix their workspace before dispatching:"
    )
    for v in main_tree:
        proj = v.get("project") or "?"
        lines.append(
            f"  [{v.get('id')}] {v.get('title')} — "
            f"workspace {v.get('path')} (project {proj})"
        )
    lines.append("")
    return lines


def _coverage_for(data: dict) -> dict:
    """The coverage block for ``data``, defaulting to a zero reading.

    A synthetic dict that never set ``coverage`` (e.g. a minimal watch frame
    built directly in a test) falls back to all-zero counts: the footer always
    renders, and says we read nothing, which is the honest reading of a
    coverage-less input.
    """
    cov = data.get("coverage")
    if isinstance(cov, dict):
        return cov
    return {
        "projects": 0,
        "boards": 0,
        "cards": 0,
        "attributed": 0,
        "unreadable": 0,
        "unreadable_projects": [],
        "no_source": [],
        "cap": None,
        "ceiling": 3,
        "in_flight": 0,
        "in_flight_boards": [],
        "main_tree": 0,
        "unread_boards": [],
        "unread_cards": 0,
        "update": None,
        "watermark": None,
        "watermark_boards": [],
        "dependents": [],
    }


def _dependents_coverage(projects: list) -> list[dict]:
    """Per-project cross-repo dependents, for the standup footer.

    One entry per project that at least one OTHER registered project declares
    a ``depends_on`` link to, each carrying its own advisory notice line (see
    ``registry.dependent_notice``). Sorted by project name. Empty when no
    project in the registry has any dependents — the common, noise-free case.
    """
    entries = []
    for project in sorted(projects, key=lambda p: p.name):
        notice = registry.dependent_notice(project.name, projects)
        if notice:
            entries.append({"project": project.name, "notice": notice})
    return entries


def _render_dependents(cov: dict) -> list[str]:
    """Footer lines naming each project's cross-repo dependents.

    One line per project with dependents: ``ecofire-bc: 2 dependent
    project(s): ecofire-app, efsdriver — consider verifying they still
    work``. Empty when no project has dependents, so a fleet with no
    ``depends_on`` links stays silent — no noise in the common case.
    """
    return [f"{entry['project']}: {entry['notice']}" for entry in cov.get("dependents", [])]


def _render_coverage(cov: dict) -> list[str]:
    """The COVERAGE footer + warning lines, always printed.

    States plainly what the digest actually READ, so a digest that matched no
    card to any project is unmistakable. Returns the footer line plus any
    warning / listing lines, ready to append after the last section. The
    fleet-wide in-flight line and its warning sit beside the read footer —
    both always rendered, never behind a flag, so the pressure on the shared
    workers is always visible.
    """
    lines: list[str] = []
    lines.append("")
    lines.append(
        f"read {cov['projects']} projects | {cov['boards']} boards | "
        f"{cov['cards']} cards | {cov['attributed']} attributed | "
        f"{cov['unreadable']} unreadable"
    )
    lines.extend(_render_dependents(cov))
    orphan = _render_orphan_boards(cov)
    if orphan:
        lines.append(orphan)
    update = _render_update_notice(cov)
    if update:
        lines.append(update)
    lines.append(_render_fleet_in_flight(cov))
    lines.append(_render_watermark(cov))
    # A project with no board and no attributed cards has no card source.
    for name in cov.get("no_source", []):
        lines.append(f"  {name}: no card source configured")
    # The loud warning: cards exist on the boards but nothing was attributed.
    warning = _coverage_warning(cov)
    if warning:
        lines.append(warning)
    # Fleet-wide warning: more in flight across ALL boards than the ceiling.
    fleet_warning = _render_fleet_warning(cov)
    if fleet_warning:
        lines.append(fleet_warning)
    return lines


def _render_fleet_in_flight(cov: dict) -> str:
    """The fleet-wide in-flight footer line beside the read line.

    ``in flight: 6 cards across 5 boards (cap 3/board)`` — the total ACTIVE
    cards (running/claimed) summed across every board (which is how the per
    board cap silently multiplies), and the per-board cap READ from config.
    The cap is omitted when unknown (e.g. a synthetic frame without config).
    """
    total = int(cov.get("in_flight", 0) or 0)
    boards = cov.get("in_flight_boards") or []
    n_boards = len(boards)
    cap = cov.get("cap")
    cap_txt = f" (cap {cap}/board)" if isinstance(cap, int) else ""
    card_word = "card" if total == 1 else "cards"
    board_word = "board" if n_boards == 1 else "boards"
    return (
        f"in flight: {total} {card_word} across {n_boards} {board_word}{cap_txt}"
    )


def _render_orphan_boards(cov: dict) -> Optional[str]:
    """The orphan/legacy-board footer line, or None when everything is covered.

    ``+ 1 unread board (legacy) holding 7 card(s) — run legacy-cards``. Printed
    only when cards were read from a board that has no registry entry — a
    hidden second source of truth (the legacy ~/.hermes/kanban.db board). The
    mutation-free pointer is enough; ``legacy-cards`` owns the re-homing.
    """
    boards = cov.get("unread_boards") or []
    if not boards:
        return None
    n_cards = int(cov.get("unread_cards", 0) or 0)
    board_word = "board" if len(boards) == 1 else "boards"
    listing = ", ".join(boards)
    return (
        f"+ {len(boards)} unread {board_word} ({listing}) holding "
        f"{n_cards} card(s) — run legacy-cards"
    )


def _render_update_notice(cov: dict) -> Optional[str]:
    """The flightdeck version-drift notice footer line, or None when current.

    ``flightdeck update available: installed 0.6.0, remote 0.7.0 (run
    flightdeck update)``. Printed only when the installed flightdeck is behind
    its own remote's newest tag (a newer version exists). Absent when coverage
    carries no verdict (a synthetic frame) or when installed is current.
    """
    notice = cov.get("update")
    if not notice:
        return None
    return str(notice)


def _render_watermark(cov: dict, *, _now: Optional[Callable] = None) -> str:
    """The ``data as of <time>`` freshness line for the digest footer.

    Dates the OLDEST critical input behind the digest: the newest state-change
    each contributing board recorded, floored to the oldest of them (see
    :func:`kanban.freshness_watermark`). Renders like:

        data as of 2026-08-17 21:03 (2h ago) — boards: hscc

    When only ONE board fed the digest it is named; with several, the oldest
    (the one that set the watermark) is named. ``data as of <unknown>`` is
    printed when no contributing board yields a readable timestamp — a digest
    built on un-dateable board data says so rather than inventing a date.

    ``_now`` is the injectable clock (a test fake) so the rendered age stays
    deterministic; default wall time. This is a factual date, never a
    staleness verdict — the distinct stale alert stays reserved for read
    failures (UNREADABLE).
    """
    ts = cov.get("watermark")
    if not isinstance(ts, (int, float)) or int(ts) <= 0:
        return "data as of <unknown>"
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts)))
    board = (cov.get("watermark_boards") or [None])[0]
    core = f"data as of {when}"
    if board:
        core += f" — board: {board}"
    return f"{core} ({_age_str(int(ts), _now=_now)})"


def _render_fleet_warning(cov: dict) -> Optional[str]:
    """The warning when fleet-wide in-flight exceeds the ceiling.

    ``WARNING: 6 in flight fleet-wide (ceiling 3) — boards: a, b, c``. The
    ceiling is ``--max-fleet`` (default 3); the boards named are exactly those
    contributing in-flight cards. Below or equal to the ceiling there is no
    warning. This card only makes the invisible visible — it never pauses or
    kills anything.
    """
    total = int(cov.get("in_flight", 0) or 0)
    ceiling = int(cov.get("ceiling", 3) or 3)
    if total <= ceiling:
        return None
    boards = ", ".join(cov.get("in_flight_boards") or [])
    return (
        f"WARNING: {total} in flight fleet-wide (ceiling {ceiling}) — "
        f"boards: {boards}"
    )


def _coverage_warning(cov: dict) -> Optional[str]:
    """The loud warning when the coverage numbers are self-evidently wrong.

    ZERO cards attributed across ALL projects while cards exist on the boards
    means attribution is failing — the digest cannot be trusted. This is the
    exact state measured the night this card was filed (clean digest printed
    while five cards were in flight, because no project matched any card).
    """
    cards = cov.get("cards", 0)
    attributed = cov.get("attributed", 0)
    if cards > 0 and attributed == 0:
        return (
            f"!!! WARNING: {cards} cards exist on the board(s) but ZERO were "
            f"attributed to any project — attribution is failing and this "
            f"digest CANNOT be trusted. Check the registry board slugs."
        )
    return None


def _render_row(section: str, row: dict) -> str:
    """One digest row, formatted for its section."""
    if section == "FAILING":
        return _render_failing(row)
    if section == "DRIFT":
        return _render_drift(row)
    if section == "UNREADABLE":
        return _render_unreadable(row)
    # Card sections: NEEDS YOU / STALE / RUNNING.
    if row.get("unattributed"):
        # A card whose workspace_path matched no project's repo. Surfaced
        # distinctly so it is never silently dropped nor guessed into a project.
        title = row.get("title") or "untitled"
        r = f"  [UNATTRIBUTED] {title}"
    else:
        board = row.get("board") or "?"
        title = row.get("title") or "untitled"
        r = f"  [{board}] {title}"
    if section == "NEEDS YOU":
        r += f" (branch {row.get('branch')})"
        verify = row.get("verify")
        if verify:
            r += f"\n      VERIFY: {verify}"
    else:
        if row.get("branch"):
            r += f" (branch {row.get('branch')})"
    if section == "STALE" and row.get("kind") == "starved":
        # An empty worktree is starvation — an infrastructure problem, named
        # explicitly and kept visually distinct from an ordinary stale card.
        r += "  — STARVED: empty worktree, likely infrastructure"
    return r


def _render_failing(row: dict) -> str:
    """One FAILING row: project whose last verify FAILED, with its age.

    The project and board are named; the FAIL status is surfaced honestly with
    the last-verified age so the operator sees both *what* is broken and *how*
    stale the evidence is.
    """
    board = row.get("board") or "unknown"
    project = row.get("project") or "?"
    r = f"  [{board}] {project} — last verify FAILED"
    ts = row.get("timestamp")
    if isinstance(ts, (int, float)):
        r += f" (last verified {_age_str(ts)})"
    return r


def _render_drift(row: dict) -> str:
    """One DRIFT row: repo vs installed version, three states.

    UNKNOWN is rendered explicitly, never folded into OK — a project we could
    not check is surfaced as such, not blessed as healthy.
    """
    board = row.get("board") or "unknown"
    project = row.get("project") or "?"
    state = row.get("state") or deployment.UNKNOWN
    r = f"  [{board}] {project}"
    if state == deployment.OK:
        r += f" — OK (installed {row.get('installed')})"
    elif state == deployment.DRIFTED:
        r += (
            f" — DRIFTED (repo {row.get('repo_version')} vs "
            f"installed {row.get('installed')})"
        )
    else:  # UNKNOWN — a real state, kept distinct from OK.
        r += " — UNKNOWN (not checked)"
    return r


def _render_unreadable(row: dict) -> str:
    """One UNREADABLE row: a project whose data could not be read, and why.

    The reason is always named — a bare project name under UNREADABLE would
    be useless, and the exact cause (missing repo path vs an unopenable board
    DB) is what tells the operator what to fix.
    """
    board = row.get("board") or "unknown"
    project = row.get("project") or "?"
    return f"  [{board}] {project} — UNREADABLE: {row.get('reason')}"


def _age_str(ts: float, *, _now: Optional[Callable] = None) -> str:
    """Human age of a unix timestamp, e.g. ``3h``, ``2d``, ``51m``.

    Uses the injectable ``_now`` clock (default wall time) so the renderer
    stays deterministic under an injected clock in tests.
    """
    now = _now() if _now is not None else time.time()
    seconds = max(0, int(now) - int(ts))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


# --------------------------------------------------------------------------- #
# watch mode
# --------------------------------------------------------------------------- #

def watch_frames(
    gather: Callable[[], dict],
    *,
    interval: int,
    _sleep: Callable,
) -> Iterator[dict]:
    """Yield one frame dict per refresh in watch mode.

    Each frame is ``{"body": str, "error": str|None}`` where ``body`` is
    :func:`render_digest` of this frame's fresh data — or, when the refresh
    itself raised, the body of the LAST good frame with ``error`` set so the
    caller can keep rendering a stale-but-real digest instead of crashing.
    ``interval`` is passed to ``_sleep`` so the caller's injected sleep is
    respected (tests never really pause).

    Infinite by design; the real shell below stops on ``KeyboardInterrupt``,
    and a test drives a controlled number of frames with an injected ``_sleep``
    that raises ``KeyboardInterrupt`` after recording the interval.
    """
    last_body = ""
    while True:
        error: Optional[str] = None
        try:
            data = gather()
            body = render_digest(data)
            last_body = body
        except Exception as exc:
            # Keep the last good frame; report the transient error, keep going.
            body = last_body
            error = f"refresh failed: {exc}"
        _sleep(interval)
        yield {"body": body, "error": error}


def _sleep_seconds(seconds: float) -> None:
    """Production sleep; testable via injection in watch_frames."""
    time.sleep(seconds)


def _clear() -> None:
    """Clear the screen between frames (ANSI erase + home)."""
    sys.stdout.write("\x1b[2J\x1b[H")
    sys.stdout.flush()


def _write_frame(frame: dict, *, timestamp: str, interval: int) -> None:
    """Print one watch frame: header (time + interval), body, optional error.

    Writes to stdout as a single block so the frame renders atomically.
    """
    header = f"standup --watch  [{timestamp}]  every {interval}s"
    sep = "-" * len(header)
    out = [header, sep, frame["body"]]
    if frame["error"]:
        out.append("")
        out.append(frame["error"])
    sys.stdout.write("\n".join(out) + "\n")
    sys.stdout.flush()


def _watch(args: argparse.Namespace, registry_path: str) -> int:
    """The --watch loop: redraw the digest every `interval` seconds.

    Same renderer as one-shot. Clears the screen between frames, prints a
    timestamp + interval header, keeps the last good frame when a refresh
    fails, and exits 0 with no traceback on Ctrl-C.
    """
    interval = max(1, int(args.interval))
    now = args.now  # injectable clock (set by run(); defaults to time.time)

    def gather() -> dict:
        return gather_data(
            registry_path,
            _run=args.run,
            _now=now,
            _state=getattr(args, "state", None),
            _listdir=getattr(args, "listdir", None),
            _config=getattr(args, "config_path", None),
            _ceiling=int(getattr(args, "max_fleet", 3) or 3),
            _update_state=getattr(args, "update_state", None),
        )

    try:
        for frame in watch_frames(
            gather,
            interval=interval,
            _sleep=args.sleep,
        ):
            _clear()
            _write_frame(
                frame,
                timestamp=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now())),
                interval=interval,
            )
    except KeyboardInterrupt:
        return 0
    return 0


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #

def build_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("standup", help="the daily digest (one-shot or --watch)",
                       epilog="example: flightdeck standup")
    p.add_argument(
        "--watch",
        action="store_true",
        help="redraw the digest every --interval seconds until interrupted",
    )
    p.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL,
        metavar="N",
        help=f"seconds between watch frames (default: {DEFAULT_INTERVAL})",
    )
    p.add_argument(
        "--max-fleet",
        type=int,
        default=3,
        metavar="N",
        help="fleet-wide warning ceiling for in-flight cards across ALL boards "
        "(default: 3); the digest warns when the total exceeds it",
    )
    p.set_defaults(func=cmd_standup)


def cmd_standup(args: argparse.Namespace, registry_path: str) -> int:
    """Run a one-shot digest, or enter watch mode when --watch is given.

    ``--json`` (the global flag set by cli.py) emits a stable machine-readable
    shape instead of the human renderer — the scripting contract for the digest.
    """
    if args.watch:
        return _watch(args, registry_path)

    data = gather_data(
        registry_path,
        _run=args.run,
        _now=args.now,
        _state=getattr(args, "state", None),
        _listdir=getattr(args, "listdir", None),
        _config=getattr(args, "config_path", None),
        _ceiling=int(getattr(args, "max_fleet", 3) or 3),
        _update_state=getattr(args, "update_state", None),
    )
    if getattr(args, "json", False):
        print(json.dumps(_render_json(data)))
    else:
        print(render_digest(data))
    return 0


def _render_json(data: dict) -> dict:
    """The stable JSON shape for the digest.

    Each section is a list of plain dicts; every key is consistently present
    (missing optional values as None) so a script can rely on the shape across
    runs. Drift rows always carry the three-state verdict with UNKNOWN distinct
    from OK; stale rows carry ``kind`` (``stale``/``starved``); every card row
    carries board + id + title + status + branch so no row is ever silently
    dropped from output.
    """
    return {
        "needs_you": [
            {
                "id": r.get("id"),
                "board": r.get("board"),
                "title": r.get("title"),
                "status": r.get("status"),
                "branch": r.get("branch"),
                "assignee": r.get("assignee"),
                "verify": r.get("verify"),
            }
            for r in data.get("needs_you", [])
        ],
        "failing": [
            {
                "project": r.get("project"),
                "board": r.get("board"),
                "status": r.get("status"),
                "timestamp": r.get("timestamp"),
                "duration_s": r.get("duration_s"),
            }
            for r in data.get("failing", [])
        ],
        "stale": [
            {
                "id": r.get("id"),
                "board": r.get("board"),
                "title": r.get("title"),
                "status": r.get("status"),
                "branch": r.get("branch"),
                "assignee": r.get("assignee"),
                "kind": r.get("kind"),
            }
            for r in data.get("stale", [])
        ],
        "running": [
            {
                "id": r.get("id"),
                "board": r.get("board"),
                "title": r.get("title"),
                "status": r.get("status"),
                "branch": r.get("branch"),
                "assignee": r.get("assignee"),
                "unattributed": bool(r.get("unattributed")),
            }
            for r in data.get("running", [])
        ],
        "drift": [
            {
                "project": r.get("project"),
                "board": r.get("board"),
                "repo_version": r.get("repo_version"),
                "installed": r.get("installed"),
                "state": r.get("state"),
            }
            for r in data.get("drift", [])
        ],
        "unreadable": [
            {
                "project": r.get("project"),
                "board": r.get("board"),
                "reason": r.get("reason"),
            }
            for r in data.get("unreadable", [])
        ],
        # The same coverage numbers the text footer carries — --json must agree
        # with the human render.
        "coverage": _coverage_for(data),
    }


def run(args: argparse.Namespace, registry_path: str) -> int:
    """Entry from cli.py: attach injectable handles, then dispatch.

    ``args.run`` / ``args.now`` / ``args.sleep`` default to the real git runner,
    real clock, and real sleep; tests set them to fakes so nothing here touches
    git, a board, or real time. ``args.state`` (verify state file) and
    ``args.listdir`` (worktree inspection) default to the real ones unless a
    test injects a fake.
    """
    args.registry = registry_path
    args.run = getattr(args, "run", None)
    args.now = getattr(args, "now", None) or time.time
    args.sleep = getattr(args, "sleep", None) or _sleep_seconds
    args.state = getattr(args, "state", None)
    args.listdir = getattr(args, "listdir", None)
    args.config_path = getattr(args, "config_path", None)
    args.update_state = getattr(args, "update_state", None)
    args.max_fleet = int(getattr(args, "max_fleet", 3) or 3)
    return cmd_standup(args, registry_path)
