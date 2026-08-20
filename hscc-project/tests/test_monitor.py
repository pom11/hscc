"""Tests for `flightdeck monitor` — live cluster activity view.

The contract that matters: a single injectable seam (:func:`_tick`) drives
every refresh — it takes a card-reader callable (returns the per-refresh
dataset) plus a clock/sleep callable, so the suite can run N ticks
deterministically with a fake clock and NEVER really sleep or touch a live
board. Card discovery itself is the production reader (:func:`_reader`), which
reuses ``kanban.list_cards``/``kanban.project_for_card`` and is tested for
per-board resilience.

Every external surface (registry, kanban board, clock, sleep) is injected — no
test touches a real board, repo, the network, or real time.
"""

import argparse

import pytest

from flightdeck.commands import monitor as cmd
from flightdeck.core import kanban, registry

NOW = 1_700_000_000
HR = 3600

# Sentinel so ``started_at=None`` (a genuinely absent timestamp) is
# distinguishable from "not provided, use the default".
_MISSING = object()


def _project(name="hscc", board="hscc", repo="/repo"):
    return registry.Project(name=name, repo=repo, board=board)


def _acard(cid, title="task", status="running", board="hscc",
           started_at=_MISSING, workspace_path="/repo/.worktrees/x"):
    """A card in cloned form (as ``kanban.list_cards`` would return)."""
    return {
        "id": cid,
        "title": title,
        "status": status,
        "board": board,
        "assignee": "coder",
        "started_at": NOW - HR if started_at is _MISSING else started_at,
        "workspace_path": workspace_path,
    }


def _data(active=None, unreadable=None, boards_error=None, projects=None):
    return {
        "active": active or [],
        "unreadable": unreadable or [],
        "boards_error": boards_error,
        "projects": projects if projects is not None else [_project()],
    }


def _args(**overrides):
    base = {
        "registry": "/tmp/reg.yaml",
        "time": 5,
        "clock": None,
        "sleep": None,
        "reader": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


# --------------------------------------------------------------------------- #
# rendering — the idle case
# --------------------------------------------------------------------------- #


def test_render_idle_line_when_nothing_active():
    out = cmd.render(_data(), now=NOW)
    assert "cluster idle" in out
    assert "nothing active" in out
    # Not an empty screen — a readable one-line statement.
    assert len(out.splitlines()) == 1


def test_render_groups_active_cards_and_omits_elapsed_when_no_timestamp():
    data = _data(
        active=[_acard("t1"), _acard("t2", status="claimed", started_at=None)],
        projects=[_project()],
    )
    out = cmd.render(data, now=NOW)
    assert "hscc  (2 active)" in out
    assert "t1" in out and "t2" in out
    assert "running" in out and "claimed" in out
    # started_at present on t1 -> elapsed rendered.
    assert "1h 0m" in out
    # started_at missing on t2 -> its line has no elapsed suffix, never guessed.
    t2_line = next(ln for ln in out.splitlines() if ln.startswith("    t2"))
    assert "·" not in t2_line


# --------------------------------------------------------------------------- #
# _tick — the injectable seam: N ticks, deterministic, no real sleep
# --------------------------------------------------------------------------- #


def test_multiple_ticks_render_and_sleep_with_interval():
    """Calling _tick N times renders N frames and sleeps once per tick with the
    configured interval — driven by a fake clock and a recording fake sleep,
    so no real time passes and no live board is touched."""
    reader = lambda: _data(active=[_acard("t1")])
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)

    frame1 = cmd._tick(reader, interval=5, clock=lambda: NOW, sleep=fake_sleep)
    frame2 = cmd._tick(reader, interval=5, clock=lambda: NOW, sleep=fake_sleep)

    assert "cluster idle" not in frame1 and "cluster idle" not in frame2
    assert "t1" in frame1 and "t1" in frame2
    # One sleep per tick, each with the configured interval.
    assert sleeps == [5, 5]


def test_time_is_honored_via_sleep_call_count():
    """`--time N` reaches the sleep callable as N. With --time 7, each tick
    sleeps 7s (recorded, not really slept)."""
    reader = lambda: _data()
    sleeps = []

    cmd._tick(reader, interval=7, clock=lambda: NOW, sleep=sleeps.append)
    cmd._tick(reader, interval=7, clock=lambda: NOW, sleep=sleeps.append)
    assert sleeps == [7, 7]


# --------------------------------------------------------------------------- #
# per-board failure resilience
# --------------------------------------------------------------------------- #


def test_per_board_read_failure_is_reported_and_loop_continues():
    """A board that raises KanbanError is reported as ``(unreadable: ...)`` and
    does not kill the tick — the other boards' cards still render."""

    def broken_board_reader():
        raise kanban.KanbanError("database is locked")

    def good_board_reader():
        return [_acard("t1")]

    boards = iter(["good", "broken", "good2"])

    def fake_list_boards():
        return list(boards) if False else ["good", "broken", "good2"]

    def fake_list_cards(board):
        if board == "broken":
            raise kanban.KanbanError("database is locked")
        return [_acard("t1", board=board)]

    # Patch the kanban functions the production reader calls.
    pg = cmd.kanban
    orig_boards, orig_cards = pg.list_boards, pg.list_cards
    pg.list_boards = fake_list_boards
    pg.list_cards = fake_list_cards
    try:
        data = cmd._reader("/tmp/reg.yaml")
    finally:
        pg.list_boards, pg.list_cards = orig_boards, orig_cards

    assert ["broken"] == data["unreadable"]
    assert data["boards_error"] is None
    # Two good boards contributed cards; the broken one did not stop them.
    assert len(data["active"]) == 2
    out = cmd.render(data, now=NOW)
    assert "(unreadable: broken)" in out
    assert out.count("t1") == 2


def test_board_enumeration_failure_is_reported_not_crashed():
    """Even a total failure to list boards degrades to a clear idle line."""
    pg = cmd.kanban
    orig_boards = pg.list_boards
    pg.list_boards = lambda: (_ for _ in ()).throw(kanban.KanbanError("no db"))
    try:
        data = cmd._reader("/tmp/reg.yaml")
    finally:
        pg.list_boards = orig_boards

    assert data["active"] == []
    assert data["boards_error"] is not None
    out = cmd.render(data, now=NOW)
    assert "cluster idle" in out
    assert "could not read boards" in out


def test_one_bad_board_does_not_kill_the_tick_loop():
    """A reader that intermittently raises per-board keeps producing frames for
    the remaining boards across multiple ticks."""
    reads = {"n": 0}

    def flaky_reader():
        reads["n"] += 1
        data = _data(active=[_acard("t1")])
        if reads["n"] == 2:  # one transient bad board on the 2nd tick
            data = _data(active=[_acard("t1")], unreadable=["broken"])
        return data

    sleeps = []
    f1 = cmd._tick(flaky_reader, interval=5, clock=lambda: NOW, sleep=sleeps.append)
    f2 = cmd._tick(flaky_reader, interval=5, clock=lambda: NOW, sleep=sleeps.append)
    assert "t1" in f1 and "t1" in f2
    assert "(unreadable: broken)" in f2
    assert sleeps == [5, 5]


# --------------------------------------------------------------------------- #
# Ctrl-C — exits 0, no traceback
# --------------------------------------------------------------------------- #


def test_ctrl_c_exits_zero_without_traceback(capsys):
    """Ctrl-C into the monitor loop returns 0 and never leaks a traceback."""
    def fake_sleep(seconds):
        raise KeyboardInterrupt

    rc = cmd.cmd_monitor(_args(sleep=fake_sleep), "/tmp/reg.yaml")
    assert rc == 0
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err


# --------------------------------------------------------------------------- #
# command discovery + argparse
# --------------------------------------------------------------------------- #


def test_monitor_is_a_discovered_command():
    from flightdeck.cli import build_parser

    args = build_parser().parse_args(["monitor", "--time", "9"])
    assert args.command == "monitor"
    assert args.func is not None
    assert args.time == 9


def test_time_defaults_to_five():
    from flightdeck.cli import build_parser

    args = build_parser().parse_args(["monitor"])
    assert args.time == 5


def test_monitor_is_not_an_mcp_tool():
    """The monitor loop must not leak into the MCP tool surface (it is an
    interactive terminal loop, not a single-shot query)."""
    from flightdeck import mcp_server
    registered = {t.name for t in mcp_server.mcp._tool_manager.list_tools()}
    assert not any(name.startswith("flightdeck_monitor") for name in registered)
