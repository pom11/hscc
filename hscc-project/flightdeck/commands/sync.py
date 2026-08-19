"""sync.py — `flightdeck project sync`: adopt existing repos/topics/boards.

The counterpart to `project new`: instead of hand-writing the registry, this
discovers what already exists and proposes how to wire it together. Discovery
happens on THREE independent sources, each owned by an existing core module:

- **repos**   — git repositories under the configured roots (default `~/dev`),
                verified through :mod:`flightdeck.core.git_state`.
- **topics**  — Telegram forum topics in the HSCC group, through
                :mod:`flightdeck.core.telegram`.
- **boards**  — Hermes kanban board slugs, through
                :mod:`flightdeck.core.kanban`.

The output is a PROPOSAL, never a silent write: `--apply` writes only the
unambiguous matches.

THE TRAP this card exists to solve — **topic names are unreliable.** Telegram
forum titles get OVERWRITTEN by bot message text on active topics, so naive
name matching fails precisely for the projects the operator uses most. The
correlation order is therefore fixed:

1. an existing registry binding (by topic id) always wins — never overwritten
   by a guess;
2. normalised name match for topics whose names still look like names;
3. anything left over is reported, never guessed.

Topics whose current title looks like message text rather than a name are
flagged NAME-CORRUPTED, with the registry's expected name and the exact
`flightdeck topics rename` command to restore each.

Every external call is injectable (``_run`` / ``_client`` / board discovery)
so tests never touch git, Telegram or the cluster.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ..core import git_state, kanban, registry, telegram
from ..core.telegram import TelegramError, TopicLockedError

DEFAULT_ROOT = "~/dev"

# Telegram's built-in "General" topic (id 1) is never a project topic — it is
# the group's default catch-all thread. It is ignored by DEFAULT so sync does
# not report it as an orphan topic forever.
DEFAULT_IGNORED_TOPICS: frozenset[int] = frozenset({1})

# Separators folded by normalization: anything that is not a letter or digit.
_SEP = re.compile(r"[^a-z0-9]+")

# A known Telegram bot message prefix. Topic titles that start with one of
# these are overwhelmingly overwritten message text, not a project name.
_BOT_PREFIXES = (
    "acknowledged",        # "Acknowledged — v1.8.1 completes the alias work…"
    "self-improvement",    # "🖾 Self-improvement review: …"
    "self_improvement",
    "review:",
    "automated",
    "released",
    "deployed",
)

# An emoji leading a topic title is handled by :func:`_emoji_lead` (code-point
# ranges — stdlib ``re`` has no ``\\p{}`` syntax, so it is not done in regex).
# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #


def normalize(name: str) -> str:
    """Fold a name to a comparable project key.

    Lowercases and folds runs of separators (space, underscore, dot, hyphen,
    and any other non-alphanumeric) into a single hyphen, then strips edge
    hyphens. Examples::

        "HSCC cluster"            -> "hscc-cluster"
        "EcoFire_customizations"  -> "ecofire-customizations"
        "app.ecofire.ro"          -> "app-ecofire-ro"
        "Flosana.es"              -> "flosana-es"
    """
    return _SEP.sub("-", str(name).lower()).strip("-")


def names_match(a: str, b: str) -> bool:
    """True when two names are the same project modulo case/separators/prefix.

    Compares the normalized forms. Two names match when they are equal, or when
    one is a strict token-prefix of the other ("hscc" is the same project as
    "HSCC cluster"; "sphoin" the same as "sphoin_engine"). Prefix folding is
    what lets a short repo basename bind to a more descriptive human topic
    title.
    """
    na = normalize(a)
    nb = normalize(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    return na.startswith(nb + "-") or nb.startswith(na + "-")


def _repo_key(repo: str) -> str:
    """Normalized project key for a repo path (its basename)."""
    return normalize(Path(repo).name)


def looks_corrupted(name: str) -> bool:
    """True if a topic title looks like MESSAGE TEXT rather than a name.

    Heuristics — a name is suspect when it is very long, contains a newline,
    starts with a known bot prefix, or starts with an emoji+colon pattern.
    Quiet topics keep their names; busy ones lose them, and this is how sync
    (and the audit) see the decay.
    """
    if not name:
        return True
    if len(name) > 60:
        return True
    if "\n" in name:
        return True
    stripped = name.lstrip()
    lowered = stripped.lower()
    if any(lowered.startswith(p) for p in _BOT_PREFIXES):
        return True
    if _emoji_lead(stripped):
        return True
    return False


def _emoji_lead(s: str) -> bool:
    """True if the string opens with one or two emoji/symbol characters.

    Telegram bot posts that clobber a topic name frequently open with an emoji
    (e.g. "🖾 Self-improvement review: …"). We only look at the leading run so
    a project name that merely *contains* an emoji later in the string is not
    misflagged. Uses code points (no ``\\p{}`` syntax, which stdlib ``re``
    does not support).
    """
    lead = s[:2]
    for ch in lead:
        cp = ord(ch)
        # Emoji / pictographs / dingbats live above U+1F000 (SMP), plus a few
        # symbol blocks (dingbats U+2700–U+27BF, misc symbols U+2600–U+26FF).
        if 0x1F000 <= cp <= 0x1FAFF:
            return True
        if 0x2700 <= cp <= 0x27BF or 0x2600 <= cp <= 0x26FF:
            return True
        if 0x2190 <= cp <= 0x21FF or 0x2B00 <= cp <= 0x2BFF:
            return True
    return False


# --------------------------------------------------------------------------- #
# Discovery (thin wrappers over the core modules — never reimplemented here)
# --------------------------------------------------------------------------- #


def discover_repos(roots: list[str], _run=None) -> list[str]:
    """Return the path of every direct child of ``roots`` that is a git repo.

    Scans one level deep under each root and keeps the entries that a real
    git repo resolves, using :func:`git_state.head_sha` (which returns None for
    a non-repo and never raises). ``_run`` is the injectable git runner.
    Non-existent roots contribute nothing.
    """
    seen: list[str] = []
    for root in roots:
        base = Path(root).expanduser()
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if not child.is_dir():
                continue
            # A worktree/`.git`-file repo still resolves a HEAD; a plain dir
            # does not. git_state decides, so we never duplicate that logic.
            if git_state.head_sha(str(child), _run=_run) is not None:
                seen.append(str(child))
    return seen


def discover_topics(_client=None) -> list[telegram.Topic]:
    """Every topic in the HSCC group as Telegram currently sees it."""
    return telegram.list_topics(_client=_client)


def discover_boards() -> list[str]:
    """Every Hermes board slug on this host."""
    return kanban.list_boards()


# --------------------------------------------------------------------------- #
# Correlation
# --------------------------------------------------------------------------- #


@dataclass
class Matched:
    """A project with all three surfaces present (or bound in the registry)."""

    name: str
    repo: str | None
    topic: int | None
    board: str | None


@dataclass
class Partial:
    """A project found on some (but not all) sources.

    ``no_board`` marks the specific, benign case where the project has a repo
    (and topic) but no board mapping — its cards are attributed by repo path,
    so a missing board is NOT a defect. When set, the renderer says so instead
    of printing an unqualified PARTIAL that reads like a gap.
    """

    name: str
    repo: str | None
    topic: int | None
    board: str | None
    no_board: bool = False


@dataclass
class Ambiguous:
    """A name that resolves to more than one repo — never auto-bound."""

    key: str
    repos: list[str]
    topics: list[int] = field(default_factory=list)
    boards: list[str] = field(default_factory=list)


@dataclass
class Conflict:
    """A free source that would overwrite an existing registry binding."""

    source: str          # "topic" | "board"
    label: str           # e.g. "topic 2046 (\"HSCC Repo\")"
    repo: str            # the repo already in the registry
    existing: str        # what the registry already ties that repo to
    proposed: str        # what sync would have written


@dataclass
class Corrupted:
    """A topic whose live title is message text, not a name."""

    topic_id: int
    current: str
    expected: str
    project: str
    rename_cmd: str


@dataclass
class BoardStatus:
    """Per-project report of its Hermes board state (report-only, no side effect).

    ``status`` is one of:
      ``exists``  — the registry names a board that Hermes actually has.
      ``missing`` — the registry names a board that Hermes does NOT have.
      ``unset``   — the registry has no board for this project at all.
    ``target_slug`` is the slug we WOULD create for this project
    (``normalize(name)``) — the basis for the ``--create-boards`` plan.
    """

    name: str
    repo: str
    registry_board: str | None
    status: str          # "exists" | "missing" | "unset"
    target_slug: str


@dataclass
class BoardConflict:
    """A project whose desired board slug is taken by an unrelated existing board.

    Reported and skipped, never silently adopted — the operator must resolve it
    explicitly (e.g. bind the project to that board or rename it) before sync
    will create anything.
    """

    name: str
    slug: str


@dataclass
class SyncReport:
    """The full correlation result. A proposal, never a side effect."""

    matched: list[Matched] = field(default_factory=list)
    partial: list[Partial] = field(default_factory=list)
    orphan_repos: list[str] = field(default_factory=list)
    orphan_topics: list[telegram.Topic] = field(default_factory=list)
    orphan_boards: list[tuple[str, int]] = field(default_factory=list)
    name_corrupted: list[Corrupted] = field(default_factory=list)
    ambiguous: list[Ambiguous] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    to_write: list[registry.Project] = field(default_factory=list)
    # Board-per-project reporting + creation plan (N9). Always computed so the
    # operator sees each project's board state; creation happens only under
    # `--create-boards --apply`.
    board_statuses: list[BoardStatus] = field(default_factory=list)
    board_conflicts: list[BoardConflict] = field(default_factory=list)
    boards_to_create: list[BoardStatus] = field(default_factory=list)


def _topic_map(projects: list[registry.Project]) -> dict[int, registry.Project]:
    return {p.topic: p for p in projects if p.topic is not None}


def _repo_map(projects: list[registry.Project]) -> dict[str, registry.Project]:
    """repo path (expanded, normalized) -> project."""
    return {git_state_head_norm(p.repo): p for p in projects}


def git_state_head_norm(repo: str) -> str:
    """Normalise a stored repo path for comparison (expand + trailing slash)."""
    r = str(Path(repo).expanduser())
    return r.rstrip("/")


def _board_map(projects: list[registry.Project]) -> dict[str, registry.Project]:
    return {p.board: p for p in projects if p.board}


def run_sync(
    *,
    repos: list[str],
    topics: list[telegram.Topic],
    boards: list[str],
    projects: list[registry.Project],
    board_cards: dict[str, int] | None = None,
    ignored_topics: set[int] | None = None,
) -> SyncReport:
    """Correlate the three discovery sources with the existing registry.

    Pure decision logic — no I/O. All inputs are plain data, so it is fully
    testable against stubbed sources. Returns a :class:`SyncReport` describing
    what sync would do; the caller (the CLI) applies it only with ``--apply``.

    ``ignored_topics`` are topic ids the operator has flagged as known-permanent
    (persisted in the registry under a top-level ``ignored_topics`` list); they
    are suppressed from reporting. :data:`DEFAULT_IGNORED_TOPICS` (Telegram's
    built-in ``General`` topic) is always included on top of that set.

    Correlation order (the contract):
      1. existing registry binding (by topic id) always wins;
      2. normalised name match for topics whose names still look like names;
      3. everything left over is reported, never guessed.
    """
    report = SyncReport()

    ignored = DEFAULT_IGNORED_TOPICS | (ignored_topics or set())

    topic_map = _topic_map(projects)
    repo_map = _repo_map(projects)
    board_map = _board_map(projects)
    repo_key_of = {r: _repo_key(r) for r in repos}

    # ------------------------------------------------------------------ #
    # 1. Existing registry bindings always win — never overwritten by a   #
    #    guess. Bound projects are folded into matched/partial and never  #
    #    re-proposed. Bound topics are audited for corruption.            #
    # ------------------------------------------------------------------ #
    bound_repos = [r for r in repos if git_state_head_norm(r) in repo_map]
    free_repos = [r for r in repos if git_state_head_norm(r) not in repo_map]
    free_topics: list[telegram.Topic] = []
    for t in sorted(topics, key=lambda x: x.id):
        if t.id in topic_map:
            _record_bound_topic(report, t, topic_map[t.id])
        elif t.id in ignored:
            # Known-permanent topic (e.g. Telegram's General, or one flagged via
            # --ignore-topic). It is never correlated and never reported as an
            # orphan, so it stops surfacing forever. A BOUND topic is never
            # ignored — an existing registry entry always wins.
            continue
        else:
            free_topics.append(t)
    free_boards = [b for b in boards if b not in board_map]

    # Every bound project is reported as matched/partial and never re-written.
    repo_paths = {git_state_head_norm(r) for r in repos}
    for p in sorted(projects, key=lambda x: x.name):
        dims = {
            "repo": git_state_head_norm(p.repo) in repo_paths,
            "topic": p.topic is not None and p.topic in {t.id for t in topics},
            "board": p.board in boards,
        }
        _classify_bound(report, p, dims)

    # -- Free topics that look corrupted: cannot be name-matched. A bound one
    #    would have been handled above (name_corrupted); an unbound corrupted
    #    topic has no expected name to restore, so it surfaces as an orphan and
    #    is never guessed into a project. ----------------------------------- #
    nameable_free_topics: list[telegram.Topic] = []
    for t in free_topics:
        if looks_corrupted(t.name):
            report.orphan_topics.append(t)
        else:
            nameable_free_topics.append(t)

    # -- Name-match free repos against nameable free topics and free boards. - #
    # A source that matches more than one repo is ambiguous and never bound.   #
    repo_topics: dict[str, list[telegram.Topic]] = {r: [] for r in free_repos}
    repo_boards: dict[str, list[str]] = {r: [] for r in free_repos}

    for t in nameable_free_topics:
        candidates = [r for r in free_repos if names_match(repo_key_of[r], t.name)]
        if len(candidates) == 1:
            repo_topics[candidates[0]].append(t)
        elif len(candidates) > 1:
            report.ambiguous.append(
                Ambiguous(key=normalize(t.name), repos=candidates, topics=[t.id])
            )
        else:
            report.orphan_topics.append(t)  # no repo to attach to

    for b in free_boards:
        candidates = [r for r in free_repos if names_match(repo_key_of[r], b)]
        if len(candidates) == 1:
            repo_boards[candidates[0]].append(b)
        elif len(candidates) > 1:
            report.ambiguous.append(Ambiguous(key=normalize(b), repos=candidates, boards=[b]))
        # len 0 -> orphan (collected below).

    # -- Conflicts: a free source matching a repo that is ALREADY bound whose
    #    dimension is already full -> report, never overwrite. ---------------- #
    for t in nameable_free_topics:
        for repo, proj in repo_map.items():
            if names_match(_repo_key(repo), t.name) and proj.topic is not None:
                report.conflicts.append(
                    Conflict(
                        source="topic",
                        label=f"topic {t.id} ({t.name!r})",
                        repo=repo,
                        existing=f"topic {proj.topic}",
                        proposed=f"bind to topic {t.id}",
                    )
                )
    for b in free_boards:
        for repo, proj in repo_map.items():
            if names_match(_repo_key(repo), b) and proj.board:
                report.conflicts.append(
                    Conflict(
                        source="board",
                        label=f"board {b!r}",
                        repo=repo,
                        existing=f"board {proj.board!r}",
                        proposed=f"bind to board {b!r}",
                    )
                )

    # -- Assemble per-repo results for free repos. --------------------------- #
    unresolved_ambiguous = {r for amb in report.ambiguous for r in amb.repos}
    for r in free_repos:
        if r in unresolved_ambiguous:
            # This repo is a candidate in an ambiguous match — it has no safe
            # single project; it is surfaced via the ambiguous line, never as
            # an orphan (it DOES relate to something) and never written.
            continue
        tlist = repo_topics[r]
        blist = repo_boards[r]
        name = Path(r).name
        if tlist and blist:
            report.matched.append(
                Matched(name=name, repo=r, topic=tlist[0].id, board=blist[0])
            )
            report.to_write.append(_new_project(name, r, tlist[0], blist[0]))
        elif tlist or blist:
            report.partial.append(
                Partial(
                    name=name,
                    repo=r,
                    topic=(tlist[0].id if tlist else None),
                    board=(blist[0] if blist else None),
                )
            )
            if tlist:
                report.to_write.append(
                    _new_project(name, r, tlist[0], blist[0] if blist else None)
                )
        else:
            report.orphan_repos.append(r)

    # -- Orphan boards: free boards that matched no repo. -------------------- #
    matched_board_keys = {m.board for m in report.matched} | {
        pa.board for pa in report.partial if pa.board is not None
    }
    used_boards = set(board_map) | matched_board_keys | {
        b for amb in report.ambiguous for b in amb.boards
    }
    for b in sorted(set(free_boards)):
        if b not in used_boards:
            report.orphan_boards.append((b, (board_cards or {}).get(b, 0)))

    # -- Consolidate ambiguous entries: a topic and a board can each surface the
    #    same ambiguous repo pair (e.g. topic 2257 'ecofire' AND board 'ecofire'
    #    both hit ~/dev/ecofire + ~/dev/EcoFire_customizations_bc). Merge them
    #    into one line so the operator sees a single decision, not a duplicate. #
    merged: dict[tuple[str, frozenset], Ambiguous] = {}
    for amb in report.ambiguous:
        key = (amb.key, frozenset(amb.repos))
        if key in merged:
            existing = merged[key]
            existing.topics.extend(t for t in amb.topics if t not in existing.topics)
            existing.boards.extend(b for b in amb.boards if b not in existing.boards)
        else:
            merged[key] = Ambiguous(
                key=amb.key, repos=list(amb.repos), topics=list(amb.topics), boards=list(amb.boards)
            )
    report.ambiguous = list(merged.values())

    # -- Board-per-project status + creation plan (N9). Always reported; the
    #    board state and creation plan are derived from the registry + the board
    #    list already computed above. This is pure bookkeeping — it does not
    #    read, move or modify a single card.                        ---------- #
    report.board_statuses = compute_board_statuses(projects, boards)
    report.board_conflicts, report.boards_to_create = plan_board_creation(
        projects, boards
    )

    return report


def _classify_bound(report: SyncReport, p: registry.Project, dims: dict) -> None:
    """Fold a registry-bound project into matched/partial by its dimensions."""
    if dims["repo"] and dims["topic"] and dims["board"]:
        report.matched.append(Matched(name=p.name, repo=p.repo, topic=p.topic, board=p.board))
    else:
        report.partial.append(
            Partial(
                name=p.name,
                repo=p.repo if dims["repo"] else None,
                topic=p.topic if dims["topic"] else None,
                board=p.board if dims["board"] else None,
                # Repo present but no board mapping -> benign, attributed by repo
                # path. Render the qualified wording, not a bare PARTIAL defect.
                no_board=bool(dims["repo"]) and not dims["board"],
            )
        )


# --------------------------------------------------------------------------- #
# Board-per-project status + creation plan (N9)
# --------------------------------------------------------------------------- #


def compute_board_statuses(
    projects: list[registry.Project], boards: list[str]
) -> list[BoardStatus]:
    """Classify every registered project's board state.

    Pure decision logic — no I/O. For each project (sorted by name):

      ``exists``  — the registry's ``board`` slug exists on Hermes;
      ``missing`` — the registry names a board slug Hermes does NOT have;
      ``unset``   — the registry has no ``board`` at all.

    ``target_slug`` is ``normalize(name)``: the slug this project SHOULD own
    (and would get under ``--create-boards``), regardless of its current state.
    """
    board_set = set(boards)
    out: list[BoardStatus] = []
    for p in sorted(projects, key=lambda x: x.name):
        rb = p.board
        if rb and rb in board_set:
            status = "exists"
        elif rb:
            status = "missing"
        else:
            status = "unset"
        out.append(
            BoardStatus(
                name=p.name,
                repo=p.repo,
                registry_board=rb,
                status=status,
                target_slug=normalize(p.name),
            )
        )
    return out


def plan_board_creation(
    projects: list[registry.Project], boards: list[str]
) -> tuple[list[BoardConflict], list[BoardStatus]]:
    """Decide which projects need a new board created, and which collide.

    Returns ``(conflicts, to_create)``.

    * A project that ALREADY has its board (status ``exists``) is skipped — it
      is reported and never re-created.
    * A project that lacks a board (``unset`` or ``missing``) wants a board at
      slug ``normalize(name)``.
    * If that slug already exists as a Hermes board and is owned by a DIFFERENT
      project (or by no registered project), it is reported as a CONFlict and
      skipped — never silently adopted.
    * Otherwise the project is scheduled to have a board created.

    Board creation never reads, moves or modifies existing cards — a missing
    board is just a gap in the registry/Hermes, not a card migration. No card
    is ever touched by this plan.
    """
    board_map = {p.board: p for p in projects if p.board}
    board_set = set(boards)
    conflicts: list[BoardConflict] = []
    to_create: list[BoardStatus] = []
    for st in compute_board_statuses(projects, boards):
        if st.status == "exists":
            continue  # reported + skipped, never re-created
        slug = st.target_slug
        if slug in board_set:
            # The slug already exists as a Hermes board. Never silently adopt
            # it: it either belongs to another registered project or is an
            # orphan. Only adopt on an explicit binding, not here.
            conflicts.append(BoardConflict(name=st.name, slug=slug))
            continue
        to_create.append(st)
    return conflicts, to_create


def _record_bound_topic(report: SyncReport, t: telegram.Topic, proj: registry.Project) -> None:
    """Report a bound topic. If its live name is corrupted, flag it with the
    exact command to restore it (the invisible decay made a one-line fix)."""
    if not looks_corrupted(t.name):
        return
    expected = proj.topic_name or proj.name
    report.name_corrupted.append(
        Corrupted(
            topic_id=t.id,
            current=t.name,
            expected=expected,
            project=proj.name,
            rename_cmd=f"flightdeck topics rename {t.id} {expected!r}",
        )
    )


def _new_project(
    name: str, repo: str, topic: telegram.Topic, board: str | None
) -> registry.Project:
    """Build a registry Project for an unambiguous new match (to write on apply)."""
    return registry.Project(
        name=name,
        repo=repo,
        topic=topic.id,
        topic_name=topic.name,
        board=board,
    )


# --------------------------------------------------------------------------- #
# Application (only under --apply)
# --------------------------------------------------------------------------- #


def apply_writes(to_write: list[registry.Project], path: str | None) -> list[registry.Project]:
    """Persist new unambiguous matches to the registry.

    Only APPENDS projects whose name is not already present, and never touches
    an existing entry — sync never overwrites an existing binding. Returns the
    list of projects actually written.
    """
    projects = registry.load_registry(path)
    existing = {p.name for p in projects}
    written: list[registry.Project] = []
    for proj in to_write:
        if proj.name in existing:
            continue  # conflict/duplicate already surfaced by the report
        projects.append(proj)
        existing.add(proj.name)
        written.append(proj)
    if written:
        registry.save_registry(projects, path)
    return written


def apply_create_boards(
    statuses: list[BoardStatus],
    path: str | None,
    *,
    _kdb=None,
) -> list[str]:
    """Create a board for each project in ``statuses`` and record it in the registry.

    For every project, calls ``kanban.create_board(slug=target_slug, name=name,
    default_workdir=repo)`` — setting ``default_workdir`` to the repo is what
    makes new cards land in the right worktree — then writes ``board:
    <target_slug>`` into that project's registry entry.

    Safety contract (N9):
      * only called under ``--create-boards --apply``;
      * each entry already passed :func:`plan_board_creation`, so its slug is
        not an existing Hermes board (no collision, no re-create);
      * creates exactly the missing boards, nothing else — a board whose
        project already has one is never in ``statuses``;
      * NEVER reads, moves or modifies a single existing card; a new board is
        an empty DB. Historical cards stay on their current board.

    Returns the list of slugs actually created.
    """
    created: list[str] = []
    for st in statuses:
        kanban.create_board(
            st.target_slug,
            name=st.name,
            default_workdir=st.repo,
            _kdb=_kdb,
        )
        # Record the binding so the project's cards now land on its own board.
        registry.set_board(st.name, st.target_slug, path=path)
        created.append(st.target_slug)
    return created


def _reflect_created_boards(report: SyncReport, created: list[str]) -> None:
    """Reflect boards created on this run back into the report (N10).

    The report is rendered from state captured BEFORE the creation step, so
    without this a project whose board was just created would still render
    ``board —`` / the "bind one with" hint and its BOARDS row would still read
    ``UNSET`` — a report that contradicts what the command just did. After this
    call each created project renders ``board <slug> (created)`` in the BOARDS
    block and, in its matched/partial row, shows the board it now owns instead
    of ``—``.

    ``created`` is the list of slugs returned by :func:`apply_create_boards`
    (empty for dry-run). Mutates ``report`` in place; no side effects.
    """
    if not created:
        return
    created_set = set(created)
    name_to_slug: dict[str, str] = {}
    for st in report.board_statuses:
        if st.target_slug in created_set:
            st.status = "created"
            st.registry_board = st.target_slug
            name_to_slug[st.name] = st.target_slug
    if not name_to_slug:
        return
    # Per-project matched/partial rows: a project whose board was just created
    # no longer renders `board —` or the "bind one with" hint; it shows the
    # board it now owns.
    for p in report.partial:
        if p.name in name_to_slug:
            p.board = name_to_slug[p.name]
            p.no_board = False
    for m in report.matched:
        if m.name in name_to_slug:
            m.board = name_to_slug[m.name]


# --------------------------------------------------------------------------- #
# Presentation + CLI
# --------------------------------------------------------------------------- #


def _fmt_topic(t: telegram.Topic) -> str:
    return f"{t.id} {t.name!r}"


def _border(name: str) -> str:
    return name


def render(report: SyncReport, board_cards: dict[str, int] | None = None) -> str:
    """Human-readable proposal text."""
    lines: list[str] = []

    if report.ambiguous:
        lines.append(f"AMBIGUOUS ({len(report.ambiguous)})")
        for amb in report.ambiguous:
            repos = " OR ".join(f"{r!r}" for r in amb.repos)
            lines.append(f"  {amb.key:<12} repo {repos}")
            # Resolution: sync never guesses which repo is the real project — a
            # wrong binding sends work to the wrong topic. The operator picks
            # the winner explicitly; give the exact command for the chosen repo.
            lines.append(
                f"      resolve: `flightdeck project new {amb.key} --repo '<chosen repo>' --apply`"
            )
        lines.append("  (ambiguous: resolve before binding — every candidate repo\n   matches by name; never auto-rebound)")
        lines.append("")

    if report.matched:
        lines.append(f"MATCHED ({len(report.matched)})")
        for m in sorted(report.matched, key=lambda x: x.name):
            lines.append(
                f"  {m.name:<12} repo {'✓' if m.repo else '—':<3} "
                f"topic {(m.topic if m.topic is not None else '—')}  "
                f"board {m.board or '—'}"
            )
        lines.append("")

    if report.partial:
        lines.append(f"PARTIAL ({len(report.partial)})")
        for p in sorted(report.partial, key=lambda x: x.name):
            lines.append(
                f"  {p.name:<12} repo {'✓' if p.repo else '—':<3} "
                f"topic {(p.topic if p.topic is not None else '—')}  "
                f"board {p.board or '—'}"
            )
            if p.no_board:
                # A repo present but no board mapping is NOT a defect — cards
                # are attributed by repo path. Say so precisely so the PARTIAL
                # does not read like a broken project.
                lines.append(
                    f"      no board mapping (cards attributed by repo path; "
                    f"bind one with `flightdeck project new {p.name} --repo {p.repo!r}`)"
                )
        lines.append("")

    if report.conflicts:
        lines.append(f"CONFLICTS ({len(report.conflicts)})")
        for c in report.conflicts:
            lines.append(f"  {c.label} matches {c.repo!r} which is already bound to {c.existing}")
        lines.append("  (existing bindings are never overwritten by sync)")
        lines.append("")

    if report.orphan_repos:
        lines.append(f"ORPHAN REPOS ({len(report.orphan_repos)})")
        for r in sorted(report.orphan_repos):
            lines.append(f"  {r}")
        lines.append("")

    if report.orphan_topics:
        lines.append(f"ORPHAN TOPICS ({len(report.orphan_topics)})")
        for t in sorted(report.orphan_topics, key=lambda x: x.id):
            lines.append(f"  {_fmt_topic(t)}")
        lines.append("")

    if report.orphan_boards:
        cards = board_cards or {}
        lines.append(f"ORPHAN BOARDS ({len(report.orphan_boards)})")
        for b, n in sorted(report.orphan_boards):
            lines.append(f"  {b:<12} ({n} card{'s' if n != 1 else ''}, no registry entry)")
        lines.append("")

    if report.name_corrupted:
        lines.append(f"NAME-CORRUPTED ({len(report.name_corrupted)})")
        for c in report.name_corrupted:
            lines.append(
                f"  topic {c.topic_id} should be {c.expected!r} ({c.project}) "
                f"but now reads {c.current!r}"
            )
            lines.append(f"    restore: {c.rename_cmd}")
        lines.append("")

    # -- Boards: per-project board state + the --create-boards plan (N9). ------ #
    if report.board_statuses:
        # `created` is the post-apply status of a board just created on this
        # run: the board now EXISTS, so it counts toward `exists` (N10) and the
        # summary agrees with the rows below.
        exists = [s for s in report.board_statuses if s.status in ("exists", "created")]
        missing = [s for s in report.board_statuses if s.status == "missing"]
        unset = [s for s in report.board_statuses if s.status == "unset"]
        lines.append(
            f"BOARDS (exists {len(exists)} · missing {len(missing)} · unset {len(unset)})"
        )
        for s in report.board_statuses:
            if s.status in ("exists", "created"):
                tag = "(EXISTS)" if s.status == "exists" else "(created)"
                lines.append(f"  {s.name:<12} board {s.registry_board!r} {tag}")
            elif s.status == "missing":
                lines.append(
                    f"  {s.name:<12} board {s.registry_board!r} (MISSING — Hermes "
                    f"has no such board; create {s.target_slug!r})"
                )
            else:
                lines.append(
                    f"  {s.name:<12} no board (UNSET; create {s.target_slug!r})"
                )
        # The creation plan block is only rendered while it is still a plan —
        # i.e. dry-run (`--create-boards` without `--apply`) or when conflicts
        # remain. After an apply that created boards, ``boards_to_create`` is
        # cleared (the rows above already say `(created)`), so this would-could
        # block disappears rather than contradict what the command just did.
        if report.boards_to_create or report.board_conflicts:
            lines.append(
                "  --create-boards --apply would create a board for exactly "
                "these projects (never touching a single existing card):"
            )
            if report.boards_to_create:
                lines.append(
                    "    create: "
                    + ", ".join(s.name for s in report.boards_to_create)
                )
            if report.board_conflicts:
                lines.append(
                    "    conflict (slug taken, skipped): "
                    + ", ".join(f"{c.name}->{c.slug}" for c in report.board_conflicts)
                )
            lines.append("  existing cards stay on their current board.")
        lines.append("")

    if not any(
        [
            report.matched,
            report.partial,
            report.ambiguous,
            report.orphan_repos,
            report.orphan_topics,
            report.orphan_boards,
            report.name_corrupted,
            report.conflicts,
        ]
    ):
        lines.append("nothing discovered — no repos, topics or boards found.")
    else:
        lines.append("--apply writes only the unambiguous matches above.")

    return "\n".join(lines)


def _render_json(report: SyncReport, board_cards: dict[str, int] | None = None) -> str:
    import json

    return json.dumps(
        {
            "matched": [
                {"name": m.name, "repo": m.repo, "topic": m.topic, "board": m.board}
                for m in report.matched
            ],
            "partial": [
                {"name": p.name, "repo": p.repo, "topic": p.topic, "board": p.board}
                for p in report.partial
            ],
            "orphan_repos": report.orphan_repos,
            "orphan_topics": [{"id": t.id, "name": t.name} for t in report.orphan_topics],
            "orphan_boards": [
                {"board": b, "cards": (board_cards or {}).get(b, 0)}
                for b, _ in report.orphan_boards
            ],
            "name_corrupted": [
                {
                    "topic_id": c.topic_id,
                    "current": c.current,
                    "expected": c.expected,
                    "project": c.project,
                    "rename_cmd": c.rename_cmd,
                }
                for c in report.name_corrupted
            ],
            "ambiguous": [
                {
                    "key": a.key,
                    "repos": a.repos,
                    "topics": a.topics,
                    "boards": a.boards,
                }
                for a in report.ambiguous
            ],
            "conflicts": [
                {
                    "source": c.source,
                    "label": c.label,
                    "repo": c.repo,
                    "existing": c.existing,
                    "proposed": c.proposed,
                }
                for c in report.conflicts
            ],
            "boards": {
                "statuses": [
                    {
                        "name": s.name,
                        "repo": s.repo,
                        "registry_board": s.registry_board,
                        "status": s.status,
                        "target_slug": s.target_slug,
                    }
                    for s in report.board_statuses
                ],
                "conflicts": [
                    {"name": c.name, "slug": c.slug} for c in report.board_conflicts
                ],
                "to_create": [
                    {"name": s.name, "slug": s.target_slug} for s in report.boards_to_create
                ],
            },
        },
        indent=2,
    )


def cmd_sync(args: argparse.Namespace) -> int:
    """The `project sync` subcommand body. Reads discovery + registry, renders,
    and applies only unambiguous matches when ``--apply`` is given. With
    ``--create-boards --apply`` it also gives each project lacking a board its
    own Hermes board (nothing is ever created without ``--apply``)."""
    roots = args.roots or [DEFAULT_ROOT]

    # Every external call is injectable so tests stub discovery entirely.
    try:
        repos = (
            args.repos
            if args.repos is not None
            else discover_repos(roots, _run=args._run)
        )
        topics = discover_topics(_client=args.client)
        boards = discover_boards()
    except TopicLockedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except TelegramError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except kanban.KanbanError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    projects = registry.load_registry(args.registry)
    if boards and args._boards is None:
        board_cards = {b: kanban.board_card_count(b) for b in boards}
    else:
        board_cards = args._boards or {}

    # Effective ignore set = Telegram's built-in General (always) + whatever is
    # already persisted in the registry + any --ignore-topic ids on this run.
    ignored = set(registry.load_ignored_topics(args.registry))
    cli_ignored = getattr(args, "ignore_topic", None) or []
    ignored.update(cli_ignored)

    report = run_sync(
        repos=repos,
        topics=topics,
        boards=boards,
        projects=projects,
        board_cards=board_cards,
        ignored_topics=ignored,
    )

    if args.apply:
        written = apply_writes(report.to_write, args.registry)
        for proj in written:
            print(f"applied: wrote {proj.name!r} -> repo {proj.repo!r}, "
                  f"topic {proj.topic}{', board ' + str(proj.board) if proj.board else ''}")

        # --apply with --ignore-topic persists the ignore set so the topic stops
        # being reported on every future run (not just this one).
        cli_ignored = getattr(args, "ignore_topic", None) or []
        if cli_ignored:
            merged = set(registry.load_ignored_topics(args.registry))
            merged.update(cli_ignored)
            registry.save_ignored_topics(sorted(merged), args.registry)
            print(f"applied: ignored topics now {sorted(merged)}")

        # --create-boards --apply: give every project lacking a board its own.
        # Only meaningful together with --apply (no silent creation). Creating
        # a board NEVER reads/moves/modifies a single existing card — each is a
        # fresh empty DB; historical cards stay on their current board.
        create_boards = getattr(args, "create_boards", False)
        created: list[str] = []
        if create_boards:
            created = apply_create_boards(
                report.boards_to_create,
                args.registry,
                _kdb=getattr(args, "_kdb", None),
            )
            for slug in created:
                print(f"applied: created board {slug!r} and bound it to its project")
            if not created and not report.board_conflicts:
                print("applied: no boards needed creating (all projects have one).")
            if report.board_conflicts:
                for c in report.board_conflicts:
                    print(
                        f"conflict: project {c.name!r} wants slug {c.slug!r} but that "
                        f"board already exists; skipped (never silently adopted)."
                    )
    else:
        created = []

    # Reflect boards created on this run back into the report so what it prints
    # agrees with what the command just did (N10): a project whose board was
    # just created must not still render `board —` / the "bind one with" hint,
    # and its BOARDS row shows `(created)`. The boards that were created are no
    # longer "to create", so drop them from the (dry-run) plan block. Rendering
    # happens AFTER the apply/create step so the single report shows the final
    # state, not a stale pre-creation snapshot.
    _reflect_created_boards(report, created)
    if created:
        report.boards_to_create = []

    if getattr(args, "json", False):
        print(_render_json(report, board_cards))
    else:
        print(render(report, board_cards))

    # Exit nonzero if there's anything the operator must look at (so scripts /
    # the daily summary can key on it), but a clean sync exits 0.
    # `(apply and not report.to_write)` flags an apply that wrote no matches.
    # It is suppressed under `--create-boards`, where the success signal is
    # board creation (or a clean idempotent no-op), not a registry match write;
    # what the operator must resolve there is `report.board_conflicts`.
    create_boards = getattr(args, "create_boards", False)
    if (
        report.orphan_repos
        or report.orphan_topics
        or report.orphan_boards
        or report.ambiguous
        or report.name_corrupted
        or report.conflicts
        or report.board_conflicts
        or (
            args.apply
            and not report.to_write
            and not create_boards
        )
    ):
        return 1
    return 0


def _project_subparsers(sub: argparse._SubParsersAction):
    """Locate the subparsers action of the EXISTING `project` group.

    ``sync`` is a subcommand of the ``project`` group, which is owned by
    :mod:`flightdeck.commands.project` (F6) — not by this module. Because
    cli.py auto-discovers every command module and calls each one's
    ``build_subparser(sub)`` on the same top-level ``sub`` (in sorted name
    order, so ``project`` is always registered before we run), we must extend
    THAT parser rather than register a second, colliding ``project`` group.
    argparse gives each parser at most one subparsers action, so we reach into
    the project parser's (stable-but-private) action list to append ``sync``.
    """
    project_parser = sub._name_parser_map["project"]
    for action in project_parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    raise RuntimeError("project group has no subparsers action")


def build_subparser(sub: argparse._SubParsersAction) -> None:
    """Attach the `sync` subcommand to the existing `project` group."""
    psub = _project_subparsers(sub)

    sp = psub.add_parser("sync", help="adopt existing repos/topics/boards into the registry",
                         epilog="example: flightdeck project sync --root ~/dev --apply")
    sp.add_argument(
        "--root",
        dest="roots",
        action="append",
        default=None,
        metavar="PATH",
        help=f"root to scan for repos (repeatable; default {DEFAULT_ROOT})",
    )
    sp.add_argument(
        "--apply",
        action="store_true",
        help="write only the unambiguous matches to the registry",
    )
    sp.add_argument(
        "--ignore-topic",
        dest="ignore_topic",
        action="append",
        type=int,
        default=None,
        metavar="ID",
        help=(
            "stop reporting a known-permanent topic (repeatable; persisted in "
            "the registry with --apply). Telegram's built-in General (id 1) is "
            "always ignored."
        ),
    )
    sp.add_argument(
        "--json",
        dest="json_override",
        action="store_true",
        help="emit machine-readable JSON for the sync result",
    )
    sp.add_argument(
        "--create-boards",
        dest="create_boards",
        action="store_true",
        help=(
            "give every project lacking a board its own Hermes board "
            "(only meaningful together with --apply; never moves existing cards)"
        ),
    )
    # Dispatch target + injectable secrets for tests (not user-facing flags).
    # `func` is set so project.run's generic dispatch reaches cmd_sync.
    sp.set_defaults(func=cmd_sync, repos=None, _run=None, _boards=None, _kdb=None, client=None, apply=False, ignore_topic=None, create_boards=False)


def run(args: argparse.Namespace, registry_path: str) -> int:
    """Entry from cli.py when the top-level command is dispatched to this module.

    With the auto-discovery seam, `project sync` is reached through
    :mod:`flightdeck.commands.project`'s ``run`` (the ``project`` group owner)
    via the ``func`` default we attach, so this ``run`` is not on the real path.
    It exists so the module still satisfies the discovery contract (both hooks
    present) and handles a direct call for tests / future top-level wiring.
    """
    args.registry = registry_path
    args.client = getattr(args, "client", None)
    args.json = getattr(args, "json_override", getattr(args, "json", False))
    if getattr(args, "project_cmd", None) != "sync":
        print(
            "project: unknown subcommand. Try `flightdeck project sync`.",
            file=sys.stderr,
        )
        return 2
    return cmd_sync(args)
