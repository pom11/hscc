"""monitor.py — live cluster activity view, refreshed every --time seconds.

A long-running terminal command the operator keeps in a spare terminal to watch
what the fleet is doing RIGHT NOW, across every registered project/board at
once. Each refresh redraws the screen and lists every RUNNING/CLAIMED card
(reusing ``kanban.ACTIVE_STATUSES`` — never an invented status set), grouped by
board/project, with id, title, status, assignee, and elapsed time since the card
was claimed (computed from the card's ``started_at`` timestamp when the schema
carries one; omitted, never guessed, otherwise). When nothing is active
anywhere it prints a single "cluster idle" line instead of an empty screen.

Read-only by design: this command never writes to any board.

Everything external is injectable. ``_tick(reader, ...)`` is the seam: it takes
a card-reader callable (returns the per-refresh dataset) and a clock/sleep
callable, so the test suite can run N ticks deterministically with a fake clock
and NEVER actually sleep or touch a live board. Card discovery reuses the same
reads standup.py already performs (``kanban.list_cards`` per board +
``kanban.project_for_card`` attribution) — this is a new *presentation* loop
over existing data, not a reimplementation of card discovery. A single board
that fails to read (locked/corrupt) is reported per-board as
``(unreadable: <board>)`` and the loop keeps going — one bad board never kills
the whole view. Ctrl-C exits cleanly with no traceback.

REPO: https://github.com/org/flightdeck

CLI WIRING: ``build_subparser`` registers the ``monitor`` subcommand and
``run(args, registry_path)`` is the cli.py entry point — both are
auto-discovered from this module exactly like every other command in this
package (see ``flightdeck/cli.py``). This is deliberately NOT an MCP tool: it
is an interactive terminal loop that does not return per-shot.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Callable, Optional

from ..core import kanban, registry

# Reuse standup's human age renderer for "elapsed since claimed" so the two
# commands can never drift on how a duration is formatted.
from .standup import _age_str

# Seconds between refreshes. The operator overrides via ``--time N``.
DEFAULT_INTERVAL = 5

# Reused, not reinvented: the exact status set standup/kanban use for
# "in flight". Never introduce a second status set here.
ACTIVE_STATUSES = kanban.ACTIVE_STATUSES

# Column layout for one card line. The fixed prefix leaves room for the
# variable-width project/board grouping labels above each group.
_CARD_INDENT = "    "


def _sleep_seconds(seconds: float) -> None:
    """Production pause between refreshes; testable via injection in _tick."""
    time.sleep(seconds)


def _clear() -> None:
    """Clear the screen between refreshes (ANSI erase + home)."""
    sys.stdout.write("\x1b[2J\x1b[H")
    sys.stdout.flush()


# --------------------------------------------------------------------------- #
# card reader — the injectable data seam
# --------------------------------------------------------------------------- #


def _reader(registry_path: str) -> dict:
    """Production card-reader: ACTIVE cards across every board, per-board tolerant.

    Returns the per-refresh dataset the renderer draws:

      ``{active: [card, ...], unreadable: [board, ...], boards_error: str|None,
        projects: [registry.Project, ...]}``

    ``active`` holds every card in an ACTIVE_STATUSES status across all readable
    boards. A board whose read raises ``kanban.KanbanError`` is recorded in
    ``unreadable`` and skipped — its cards are genuinely unknown, but the rest of
    the fleet is still reported (one bad board never blanks the whole view). If
    even board *enumeration* fails, ``boards_error`` carries the reason and there
    are no active cards. ``projects`` is the loaded registry, so the renderer can
    attribute cards the same way standup does (``kanban.project_for_card``).
    """

    projects = registry.load_registry(registry_path)
    try:
        boards = kanban.list_boards()
    except kanban.KanbanError as exc:
        return {
            "active": [],
            "unreadable": [],
            "boards_error": str(exc),
            "projects": projects,
        }

    active: list[dict] = []
    unreadable: list[str] = []
    for board in boards:
        try:
            cards = kanban.list_cards(board=board)
        except kanban.KanbanError:
            # This board is unreadable; report it and move on to the next.
            unreadable.append(str(board))
            continue
        for card in cards:
            if str(card.get("status") or "") in ACTIVE_STATUSES:
                active.append(card)
    return {
        "active": active,
        "unreadable": unreadable,
        "boards_error": None,
        "projects": projects,
    }


# --------------------------------------------------------------------------- #
# render — deterministically turns the reader's dataset into screen text
# --------------------------------------------------------------------------- #


def _elapsed(card: dict, now: float) -> Optional[str]:
    """Human elapsed time since this card was claimed, or None.

    The claimed-at timestamp is the card's ``started_at`` (set when the worker
    claims it). When the schema carries one we format it via standup's age
    renderer; when it is missing or unusable we return None so the caller
    omits elapsed rather than guessing.
    """
    ts = card.get("started_at")
    if ts is None:
        return None
    try:
        return _age_str(int(ts), _now=lambda: now)
    except (TypeError, ValueError):
        return None


def _card_line(card: dict, now: float) -> str:
    """One card line: ``id | status | assignee | title [ · 3h]`` (elapsed if known)."""
    cid = str(card.get("id") or "?")
    status = str(card.get("status") or "?")
    assignee = str(card.get("assignee") or "-")
    title = str(card.get("title") or "").strip()

    line = f"{_CARD_INDENT}{cid}  [{status}]  {assignee:<12}  {title}"
    age = _elapsed(card, now)
    if age is not None:
        line += f"  · {age}"
    return line.rstrip()


def _group_active(active: list[dict], projects: list) -> list[tuple[str, list[dict]]]:
    """Group ACTIVE cards into ordered ``(label, cards)`` groups.

    A card attributed to a registered project (via ``kanban.project_for_card``)
    groups under that project's name; an unattributed card groups under its
    board slug. Groups keep first-seen card order, and are sorted by label so
    output is stable across refreshes. An empty list yields no groups (the
    renderer falls through to the idle line).
    """
    groups: dict[str, list[dict]] = {}
    for card in active:
        project = kanban.project_for_card(card, projects)
        if project is not kanban.UNATTRIBUTED and getattr(project, "name", None):
            label = str(project.name)
        else:
            label = str(card.get("board") or "default")
        groups.setdefault(label, []).append(card)
    return sorted((label, cards) for label, cards in groups.items())


def render(data: dict, *, now: float) -> str:
    """Render one refresh of the reader's dataset to screen text.

    ``data`` is the ``{active, unreadable, boards_error, projects}`` the
    card-reader returns. Returns a string with no trailing newline. When there
    is nothing active anywhere and nothing unreadable, returns the single
    "cluster idle" line.
    """
    out: list[str] = []
    unreadable = data.get("unreadable") or []
    boards_error = data.get("boards_error")

    # Board enumeration itself failed: nothing can be shown, say so clearly.
    if boards_error is not None:
        out.append(f"cluster idle — could not read boards: {boards_error}")

    active = data.get("active") or []
    projects = data.get("projects") or []
    groups = _group_active(active, projects)

    for label, cards in groups:
        out.append(f"{label}  ({len(cards)} active)")
        for card in cards:
            out.append(_card_line(card, now))

    for board in unreadable:
        out.append(f"  (unreadable: {board})")

    if not out:
        out.append("cluster idle — nothing active right now")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# watch loop
# --------------------------------------------------------------------------- #


def _tick(
    reader: Callable[[], dict],
    *,
    interval: int,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = _sleep_seconds,
) -> str:
    """One monitor refresh: read the fleet's active cards, render, then pause.

    ``reader`` is the injected card-reader callable (returns the per-refresh
    dataset without touching a live board in tests). ``clock`` drives the
    elapsed-time rendering so it stays deterministic under an injected fake
    clock. ``sleep`` pauses for ``interval`` seconds so the loop is testable —
    a fake sleep records the interval instead of really pausing. Returns the
    rendered frame body (the ``render`` output for this tick).
    """
    body = render(reader(), now=clock())
    sleep(interval)
    return body


def _watch(
    reader: Callable[[], dict],
    *,
    interval: int,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> int:
    """The monitor loop: redraw the view every ``interval`` seconds.

    Clears the screen between refreshes, prints the rendered frame, and
    returns 0 with no traceback on Ctrl-C (KeyboardInterrupt). Because this is
    a deliberate long-running loop, it NEVER returns on its own — only Ctrl-C
    stops it.
    """
    try:
        while True:
            _clear()
            sys.stdout.write(_tick(reader, interval=interval, clock=clock, sleep=sleep) + "\n")
            sys.stdout.flush()
    except KeyboardInterrupt:
        return 0
    return 0


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #


def build_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "monitor",
        help="live cluster activity view across all boards (refreshes every --time seconds)",
        epilog="example: flightdeck monitor --time 30",
    )
    p.add_argument(
        "--time",
        type=int,
        default=DEFAULT_INTERVAL,
        metavar="N",
        help=f"seconds between refreshes (default: {DEFAULT_INTERVAL})",
    )
    p.set_defaults(func=cmd_monitor)


def cmd_monitor(args: argparse.Namespace, registry_path: str) -> int:
    """Run the monitor loop until interrupted."""
    interval = max(1, int(getattr(args, "time", DEFAULT_INTERVAL)))
    clock = getattr(args, "clock", None) or time.time
    sleep = getattr(args, "sleep", None) or _sleep_seconds

    injected = getattr(args, "reader", None)
    if injected is not None:
        reader = injected
    else:
        def reader() -> dict:
            return _reader(registry_path)

    return _watch(reader, interval=interval, clock=clock, sleep=sleep)


def run(args: argparse.Namespace, registry_path: str) -> int:
    """Entry from cli.py: attach injectable handles, then dispatch.

    ``args.clock`` / ``args.sleep`` / ``args.reader`` default to the real clock,
    real sleep, and the production per-board card-reader; tests set them to
    fakes so nothing here touches real time or a live board.
    """
    return cmd_monitor(args, registry_path)
