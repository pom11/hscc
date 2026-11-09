"""Tests for hscc_daemon/cards_live.py — `hscc cards monitor`.

Offline only: a fake collector, a fake clock, worker logs and fake session
stores under tmp_path. NEVER the operator's live boards or ~/.hermes.
"""

import io
import json
import os
import sqlite3

import pytest

from hscc_daemon import cards_live, kanban_live
from hscc_daemon.cli_theme import make_console
from hscc_daemon.cards_live import (LogTail, Monitor, SessionTail, ViewState, clean_line,
                                    cmd_cards, fit_header, grid_shape, header_fields,
                                    is_tool_line, log_path, make_tail, render_rows,
                                    wrap_tail)

NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _no_real_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setattr(kanban_live, "HERMES_HOME", str(tmp_path / "hermes-home"))


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


def run_row(board, tid, worker="backend-engineer", run_s=120, hb_s=5, tok=(1500, 300)):
    return {"board": board, "id": tid, "title": f"title {tid}", "worker": worker,
            "started_at": int(NOW - run_s), "run_s": run_s, "hb_s": hb_s,
            "tok_in": tok[0] if tok else None, "tok_out": tok[1] if tok else None}


def snap(running=(), ready=(), events=(), cap=10, outcomes=None):
    return {"running": list(running), "ready": list(ready), "events": list(events),
            "caps": (cap, None), "errors": [], "boards": [{"name": "b"}],
            "outcomes": outcomes or {}}


def write_log(home, board, tid, text, mode="w"):
    p = log_path(board, tid, str(home))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, mode, encoding="utf-8") as f:
        f.write(text)
    return p


def make_store(path, session_id="local-abc", rows=()):
    """A worker ``state.db`` holding one kanban session and its messages.

    The columns are the live v0.23 shape — ``PRAGMA table_info(messages)`` on
    the operator's own store (t_a5c49a41): every NOT NULL column
    (``session_id``/``role``/``timestamp``/``active``/``compacted``/
    ``_compressed_summary``) plus the ones the reader selects. A fixture that
    invents its own schema proves nothing about the reader.
    """
    os.makedirs(os.path.dirname(str(path)) or ".", exist_ok=True)
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL,"
                  " model TEXT, started_at REAL NOT NULL, ended_at REAL,"
                  " message_count INTEGER, input_tokens INTEGER, output_tokens INTEGER)")
        c.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT NOT NULL,"
                  " role TEXT NOT NULL, content TEXT, tool_call_id TEXT, tool_calls TEXT,"
                  " tool_name TEXT, timestamp REAL NOT NULL, active INTEGER NOT NULL"
                  " DEFAULT 1, compacted INTEGER NOT NULL DEFAULT 0, display_kind TEXT,"
                  " display_metadata TEXT, _compressed_summary INTEGER NOT NULL DEFAULT 0)")
        c.execute("INSERT INTO sessions (id, source, started_at) VALUES (?, 'kanban', ?)",
                  (session_id, NOW))
    for r in rows:
        add_msg(path, session_id, **r)
    return str(path)


def add_msg(path, session_id, role, content=None, tool_calls=None, tool_name=None,
            tool_call_id=None, display_metadata=None, active=1, compacted=0):
    """Append one message row the way the gateway writes it (tool_calls as JSON)."""
    with sqlite3.connect(path) as c:
        c.execute("INSERT INTO messages (session_id, role, content, tool_calls, tool_name,"
                  " tool_call_id, display_metadata, timestamp, active, compacted)"
                  " VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (session_id, role, content,
                   json.dumps(tool_calls) if tool_calls is not None else None,
                   tool_name, tool_call_id, display_metadata, NOW, active, compacted))


def call(name, arguments, cid):
    """One entry of an assistant row's ``tool_calls`` JSON, as the API returns it."""
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}


def live_session_tail(tmp_path, rows=(), session_id="local-abc", **kw):
    db = make_store(tmp_path / "profiles" / "w" / "state.db", session_id, rows)
    return SessionTail(db, session_id, **kw)


# ── pure helpers ───────────────────────────────────────────────────────────

def test_log_path_default_board_is_kanban_root(tmp_path):
    assert log_path("default", "t_1", str(tmp_path)) == str(tmp_path / "kanban/logs/t_1.log")
    assert log_path("hscc", "t_1", str(tmp_path)) == \
        str(tmp_path / "kanban/boards/hscc/logs/t_1.log")


def test_clean_line_strips_ansi_and_keeps_last_cr_redraw():
    assert clean_line("\x1b[1;32mok\x1b[0m done") == "ok done"
    assert clean_line("spinner 1\rspinner 2\rfinal text  ") == "final text"
    assert clean_line("a\tb") == "a    b"


def test_tool_lines_exclude_preparing_noise():
    assert is_tool_line("  ┊ 💻 $  git status  0.1s")
    assert not is_tool_line("  ┊ 💻 preparing terminal…")
    assert not is_tool_line("plain reasoning text")


@pytest.mark.parametrize("n,expect_cells", [(1, 1), (2, 2), (4, 4), (7, 7), (10, 10)])
def test_grid_has_enough_cells_and_no_empty_column(n, expect_cells):
    cols, rows = grid_shape(n, 200, 50)
    assert cols * rows >= expect_cells
    assert cols * (rows - 1) < n           # no entirely empty row
    assert (cols - 1) * rows < n           # no entirely empty column


def test_grid_prefers_columns_on_wide_terminals_and_rows_on_tall_ones():
    assert grid_shape(2, 240, 40) == (2, 1)
    assert grid_shape(2, 60, 80) == (1, 2)
    assert grid_shape(0, 100, 40) == (0, 0)


def test_fit_header_drops_details_right_to_left():
    title, details = "hscc · t_1 · worker", ["run 5m", "beat 3s", "tok 2k/1k"]
    assert fit_header(title, details, 200).endswith("run 5m · beat 3s · tok 2k/1k")
    mid = fit_header(title, details, len(title) + len(" │ run 5m · beat 3s") )
    assert mid.endswith("beat 3s") and "tok" not in mid
    assert fit_header(title, details, len(title)) == title
    assert fit_header(title, details, 6).endswith("…") and len(fit_header(title, details, 6)) == 6


def test_wrap_tail_returns_last_visual_rows():
    lines = ["a" * 10, "short", "b" * 7]
    assert wrap_tail(lines, 4, 3) == ["t", "bbbb", "bbb"]
    assert wrap_tail(["x" * 9], 4, 10) == ["xxxx", "xxxx", "x"]
    assert wrap_tail([], 4, 3) == []
    assert wrap_tail(["abc"], 4, 0) == []


# ── log following ──────────────────────────────────────────────────────────

def test_logtail_reads_incrementally_and_keeps_partial_line(tmp_path):
    p = write_log(tmp_path, "b", "t_1", "one\ntwo\npart")
    t = LogTail(p)
    assert t.poll() is True
    assert t.view(False) == ["one", "two", "part"]
    assert t.poll() is False                       # nothing appended
    write_log(tmp_path, "b", "t_1", "ial\n  ┊ 💻 $ ls  0.1s\n", mode="a")
    assert t.poll() is True
    assert t.view(False) == ["one", "two", "partial", "  ┊ 💻 $ ls  0.1s"]
    assert t.view(True) == ["  ┊ 💻 $ ls  0.1s"]


def test_logtail_starts_at_tail_of_big_file_and_drops_fragment(tmp_path, monkeypatch):
    monkeypatch.setattr(cards_live, "INITIAL_TAIL_BYTES", 20)
    p = write_log(tmp_path, "b", "t_1", "".join(f"line{i:03d}\n" for i in range(50)))
    t = LogTail(p)
    t.poll()
    rows = t.view(False)
    assert rows[-1] == "line049"
    assert all(r.startswith("line") and len(r) == 7 for r in rows)  # no half line


def test_logtail_missing_or_truncated_file_never_raises(tmp_path):
    t = LogTail(str(tmp_path / "nope.log"))
    assert t.poll() is False and t.view(False) == []
    p = write_log(tmp_path, "b", "t_1", "aaaa\nbbbb\n")
    t = LogTail(p)
    t.poll()
    write_log(tmp_path, "b", "t_1", "new\n")      # truncated + rewritten
    t.poll()
    assert t.view(False)[-1] == "new"


# ── monitor: re-flow + linger ──────────────────────────────────────────────

def test_monitor_adds_panels_and_lingers_finished_cards(tmp_path):
    clock = Clock()
    snaps = [snap([run_row("b", "t_1"), run_row("b", "t_2")]),
             snap([run_row("b", "t_2"), run_row("c", "t_3")],
                  events=[{"board": "b", "id": "t_1", "kind": "completed", "at": NOW}])]
    mon = Monitor(clock=clock, collect=lambda: snaps[0], hermes_home=str(tmp_path))
    mon.refresh()
    assert mon.panels() == [("b", "t_1"), ("b", "t_2")]

    mon._collect = lambda: snaps[1]
    clock.t += 3
    mon.refresh()
    assert mon.panels() == [("b", "t_1"), ("b", "t_2"), ("c", "t_3")]
    assert mon.cards[("b", "t_1")]["outcome"] == "completed"

    clock.t += cards_live.LINGER_S
    mon.refresh()
    assert mon.panels() == [("b", "t_2"), ("c", "t_3")]
    assert ("b", "t_1") not in mon.tails


def test_monitor_unknown_outcome_is_stopped(tmp_path):
    clock = Clock()
    mon = Monitor(clock=clock, collect=lambda: snap([run_row("b", "t_1")]),
                  hermes_home=str(tmp_path))
    mon.refresh()
    mon._collect = lambda: snap([])
    mon.refresh()
    assert mon.cards[("b", "t_1")]["outcome"] == "stopped"


# ── MON-23-FIELDS: the outcome label a lingering panel shows ────────────────

def test_every_outcome_kind_the_board_can_deliver_has_a_label():
    """`header_fields` takes its label from `OUTCOME_STYLE`, but the kinds come
    from `kanban_live.OUTCOME_KINDS` — two files, and nothing used to check they
    agreed, so an admitted kind rendered as raw snake_case under a blanket warn
    (measured live: a panel whose last word was `terminal_worker_reaped`)."""
    missing = sorted(set(kanban_live.OUTCOME_KINDS) - set(cards_live.OUTCOME_STYLE))
    assert not missing, missing
    for kind, (label, colour) in cards_live.OUTCOME_STYLE.items():
        assert label and colour, kind
        assert " " not in label or label != kind, kind   # a real label, not the kind


def _lingering(mon, outcome_kind, events=()):
    """One card live on one tick, gone on the next, with `outcome_kind` the only
    thing the collector says about how it stopped."""
    mon.refresh()                                   # card is live
    mon._collect = lambda: snap([], events=events,
                                outcomes={"b": {"t_1": outcome_kind}})
    mon.refresh()
    return mon.cards[("b", "t_1")]


def test_a_lingering_panel_labels_the_kernels_own_alarms(tmp_path):
    for kind, want in (("terminal_worker_reaped", "worker reaped"),
                       ("protocol_violation", "protocol violation"),
                       ("suspected_hallucinated_references", "hallucinated refs"),
                       ("completion_blocked_empty_result", "empty result"),
                       ("changes_requested", "changes requested"),
                       ("review_requested", "in review")):
        mon = Monitor(clock=Clock(), hermes_home=str(tmp_path),
                      collect=lambda: snap([run_row("b", "t_1")]))
        row = _lingering(mon, kind)
        assert row["outcome"] == kind
        # the same label the panel paints, read off the header fields
        assert header_fields(row, NOW, None)[1] == [want], kind


def test_a_card_the_last_hour_panel_capped_away_still_gets_its_outcome(tmp_path):
    """`events` is the panel's newest `RECENT_LIMIT` rows shared across every
    board, so a card that stopped behind 12 busier events had no label at all and
    lingered as a bare `stopped`. `outcomes` answers for every card."""
    busier = [{"board": "other", "id": f"t_{i}", "kind": "completed", "at": NOW + i}
              for i in range(kanban_live.RECENT_LIMIT)]
    mon = Monitor(clock=Clock(), hermes_home=str(tmp_path),
                  collect=lambda: snap([run_row("b", "t_1")]))
    row = _lingering(mon, "crashed", events=busier)
    assert row["outcome"] == "crashed", row["outcome"]
    assert header_fields(row, NOW, None)[1] == ["crashed"]


def test_a_collector_without_the_outcomes_map_still_labels_from_events(tmp_path):
    """The fallback stays: an older collector (or a stub) that reports only
    `events` must keep labelling what the panel happens to show."""
    mon = Monitor(clock=Clock(), hermes_home=str(tmp_path),
                  collect=lambda: snap([run_row("b", "t_1")]))
    mon.refresh()
    mon._collect = lambda: {"running": [], "ready": [], "caps": (10, None),
                            "errors": [], "boards": [{"name": "b"}],
                            "events": [{"board": "b", "id": "t_1", "kind": "gave_up",
                                        "at": NOW}]}
    mon.refresh()
    assert mon.cards[("b", "t_1")]["outcome"] == "gave_up"


# ── rendering ──────────────────────────────────────────────────────────────

def render_text(mon, width=160, height=40, **kw):
    console = make_console(file=io.StringIO(), width=width, height=height, color_system=None)
    console.print(cards_live.render(mon, width, height, **kw), height=height)
    return console.file.getvalue()


def test_render_shows_header_details_body_and_waiting_cards(tmp_path):
    write_log(tmp_path, "hscc", "t_1", "thinking about it\n  ┊ 💻 $ pytest -q  3.1s\n")
    ready = [{"board": "hscc", "id": "t_9", "worker": "qa", "wait_s": 300,
              "title": "waiting card", "priority": 0}]
    mon = Monitor(clock=Clock(), hermes_home=str(tmp_path),
                  collect=lambda: snap([run_row("hscc", "t_1")], ready=ready))
    mon.refresh()
    mon.poll_logs()
    out = render_text(mon)
    assert "hscc · t_1 · backend-engineer" in out
    assert "run 2m" in out and "beat 5s" in out and "tok 2k/300" in out
    assert "thinking about it" in out and "pytest -q" in out
    assert "⏳ hscc · t_9 · qa · wait 5m · waiting card" in out
    assert "slots 1/10 · waiting 1" in out

    tools = render_text(mon, tools_only=True)
    assert "pytest -q" in tools and "thinking about it" not in tools


def test_render_zoom_shows_only_focused_card(tmp_path):
    mon = Monitor(clock=Clock(), hermes_home=str(tmp_path),
                  collect=lambda: snap([run_row("b", "t_1"), run_row("b", "t_2")]))
    mon.refresh()
    grid = render_text(mon)
    assert "t_1" in grid and "t_2" in grid
    zoomed = render_text(mon, focus=1, zoom=True)
    assert "b · t_2" in zoomed and "b · t_1" not in zoomed


def test_render_empty_and_finished_states(tmp_path):
    mon = Monitor(clock=Clock(), hermes_home=str(tmp_path), collect=lambda: snap([]))
    mon.refresh()
    assert "no cards running" in render_text(mon)
    mon._collect = lambda: snap([run_row("b", "t_1")])
    mon.refresh()
    mon._collect = lambda: snap([], events=[{"board": "b", "id": "t_1", "kind": "blocked",
                                             "at": NOW}])
    mon.refresh()
    assert "blocked" in render_text(mon)


def test_render_many_cards_in_small_terminal_does_not_crash(tmp_path):
    rows = [run_row("b", f"t_{i}") for i in range(10)]
    mon = Monitor(clock=Clock(), hermes_home=str(tmp_path), collect=lambda: snap(rows))
    mon.refresh()
    out = render_text(mon, width=80, height=20)
    assert "t_0" in out


# ── keys + CLI ─────────────────────────────────────────────────────────────

def test_viewstate_focus_wraps_and_toggles():
    v = ViewState()
    v.apply("prev", 3)
    assert v.focus == 2
    v.apply("next", 3)
    assert v.focus == 0
    v.apply("zoom", 3)
    assert v.zoom
    v.apply("unzoom", 3)
    assert not v.zoom
    v.apply("tools", 3)
    v.apply("pause", 3)
    assert v.tools_only and v.paused
    v.apply("next", 0)                 # no panels: focus untouched, no ZeroDivision
    v.apply("quit", 0)
    assert v.quit


def test_keymap_covers_arrows_tab_and_quit():
    km = cards_live.KEYMAP
    assert km["\t"] == "next" and km["\x1b[Z"] == "prev"
    assert km["\x1b[C"] == "next" and km["\x1b[D"] == "prev"
    assert km["q"] == "quit" and km["t"] == "tools" and km["\r"] == "zoom"


def test_cli_help_and_errors(capsys):
    assert cmd_cards([]) == 0
    assert "hscc cards monitor" in capsys.readouterr().out
    assert cmd_cards(["monitor", "--help"]) == 0
    capsys.readouterr()
    assert cmd_cards(["bogus"]) == 2
    assert cmd_cards(["monitor", "--interval", "0"]) == 2
    assert cmd_cards(["monitor", "--interval"]) == 2
    assert cmd_cards(["monitor", "--what"]) == 2
    err = capsys.readouterr().err
    assert "--interval must be >= 1" in err and "unknown argument" in err


def test_cli_snapshot_uses_collector(tmp_path, monkeypatch):
    monkeypatch.setattr(Monitor, "_default_collect",
                        lambda self: snap([run_row("b", "t_1")]))
    console = make_console(file=io.StringIO(), width=120, height=30, color_system=None)
    assert cmd_cards(["monitor", "--board", "b"], console=console) == 0
    assert "b · t_1" in console.file.getvalue()


def test_render_fills_exact_height_with_waiting_cards_at_the_bottom(tmp_path):
    ready = [{"board": "b", "id": f"t_w{i}", "worker": "qa", "wait_s": 60,
              "title": "w", "priority": 0} for i in range(5)]
    rows = [run_row("b", f"t_{i}") for i in range(4)]
    mon = Monitor(clock=Clock(), hermes_home=str(tmp_path),
                  collect=lambda: snap(rows, ready=ready))
    mon.refresh()
    lines = render_text(mon, width=120, height=30).rstrip("\n").split("\n")
    assert len(lines) == 30
    assert "slots 4/10" in lines[0]
    assert lines[-1].strip() == "+2 more waiting"
    assert "t_w2" in lines[-2]
    assert lines[1].startswith("b · t_0")     # first header rule directly under the status line


def test_plus_minus_change_columns_from_the_current_auto_grid():
    v = ViewState()
    v.apply("bigger", 6, auto_cols=3)
    assert v.cols == 2                     # bigger panels = one column fewer
    v.apply("bigger", 6, auto_cols=3)
    v.apply("bigger", 6, auto_cols=3)
    assert v.cols == 1                     # floor
    v.apply("smaller", 6, auto_cols=3)
    assert v.cols == 2
    for _ in range(10):
        v.apply("smaller", 6, auto_cols=3)
    assert v.cols == 6                     # never more columns than cards
    v.apply("auto", 6)
    assert v.cols is None
    km = cards_live.KEYMAP
    assert km["+"] == km["="] == "bigger" and km["-"] == "smaller" and km["0"] == "auto"


def test_forced_columns_drive_the_grid(tmp_path):
    assert grid_shape(6, 240, 40, cols=1) == (1, 6)
    assert grid_shape(6, 240, 40, cols=2) == (2, 3)
    assert grid_shape(3, 240, 40, cols=9) == (3, 1)
    rows = [run_row("b", f"t_{i}") for i in range(4)]
    mon = Monitor(clock=Clock(), hermes_home=str(tmp_path), collect=lambda: snap(rows))
    mon.refresh()
    out = render_text(mon, width=160, height=40, cols=1)
    assert "cols 1" in out.split("\n")[0]
    assert sum(1 for line in out.split("\n") if line.startswith("b · t_")) == 4  # 4 stacked rows


def test_cards_are_header_rules_not_boxes(tmp_path):
    write_log(tmp_path, "b", "t_0", "hello body\n")
    rows = [run_row("b", f"t_{i}") for i in range(2)]
    mon = Monitor(clock=Clock(), hermes_home=str(tmp_path), collect=lambda: snap(rows))
    mon.refresh()
    mon.poll_logs()
    out = render_text(mon, width=120, height=20)
    assert not any(ch in out for ch in "╭╮╰╯")   # (│ only as the in-title separator)
    header = out.split("\n")[1]
    assert header.startswith("b · t_0") and "─" in header and "b · t_1" in header
    assert "hello body" in out.split("\n")[2]   # body starts right under the rule


def test_title_always_gold_rule_gold_only_when_focused(tmp_path):
    rows = [run_row("b", f"t_{i}") for i in range(2)]
    mon = Monitor(clock=Clock(), hermes_home=str(tmp_path), collect=lambda: snap(rows))
    mon.refresh()
    focused = cards_live.card_panel(mon, ("b", "t_0"), 60, 10, True, False, NOW)
    idle = cards_live.card_panel(mon, ("b", "t_1"), 60, 10, False, False, NOW)
    f_rule, i_rule = focused.renderables[0].renderable, idle.renderables[0].renderable
    assert f_rule.title.style == i_rule.title.style == cards_live.TITLE_STYLE
    assert f_rule.style == cards_live.FOCUS_BORDER
    assert i_rule.style == cards_live.IDLE_BORDER


# ── the session store: where the v0.23 transcript actually is ──────────────

WORKER_ROWS = [
    dict(role="user", content="work kanban task t_1\n" + "x" * 8000),
    dict(role="assistant", content="", tool_calls=[call("kanban_show", {"task_id": "t_1"}, "c1")]),
    dict(role="tool", content="task body " * 900, tool_name="kanban_show", tool_call_id="c1"),
    dict(role="assistant", content="I'll start by reading the card."),
    dict(role="assistant", content="", tool_calls=[call("terminal", {"command": "git status"}, "c2")]),
    dict(role="tool", content="M hscc_daemon/cards_live.py", tool_name="terminal",
         tool_call_id="c2"),
]


def test_session_shows_prose_and_one_line_per_tool_call(tmp_path):
    t = live_session_tail(tmp_path, rows=WORKER_ROWS)
    assert t.poll() is True
    lines = t.view(False)
    assert lines[0].startswith("▸ work kanban task t_1 ")      # the 8 KB card prompt,
    assert len(lines[0]) == cards_live.PROMPT_PREVIEW + 3     # one collapsed preview
    assert lines[1] == "┊ kanban_show  t_1"                    # named when it was CALLED
    assert lines[2] == "I'll start by reading the card."       # the only prose row
    assert lines[3] == "┊ terminal  git status"
    assert len(lines) == 4                    # the two result rows add no lines


def test_tools_only_shows_only_the_tool_lines(tmp_path):
    t = live_session_tail(tmp_path, rows=WORKER_ROWS)
    t.poll()
    assert t.view(True) == ["┊ kanban_show  t_1", "┊ terminal  git status"]
    assert all(is_tool_line(r) for r in t.view(True))


def test_second_poll_reads_only_the_appended_rows(tmp_path):
    t = live_session_tail(tmp_path)
    assert t.poll() is False and t.view(False) == []       # store exists, no rows yet
    add_msg(t.path, t.session_id, role="assistant", content="one")
    assert t.poll() is True and t.view(False) == ["one"]
    assert t.cursor == 1
    assert t.poll() is False and t.view(False) == ["one"]   # nothing appended
    add_msg(t.path, t.session_id, role="assistant", content="two")
    add_msg(t.path, t.session_id, role="assistant", content="",
            tool_calls=[call("terminal", {"command": "ls"}, "c3")])
    assert t.poll() is True
    assert t.view(False) == ["one", "two", "┊ terminal  ls"]
    assert t.cursor == 3                                    # only the new rows were read


def test_a_rewound_store_restarts_the_tail_instead_of_raising(tmp_path):
    t = live_session_tail(tmp_path, rows=[dict(role="assistant", content=f"m{i}")
                                          for i in range(5)])
    t.poll()
    assert t.view(False) == [f"m{i}" for i in range(5)]
    with sqlite3.connect(t.path) as c:                      # rows vanished from the end
        c.execute("DELETE FROM messages WHERE id > 2")
    assert t.poll() is True                                 # restart, never raise
    assert t.view(False)[-2:] == ["m0", "m1"]


def test_unreadable_or_absent_store_is_an_empty_panel_never_a_traceback(tmp_path):
    junk = tmp_path / "state.db"
    junk.write_bytes(b"definitely not a sqlite database")
    t = SessionTail(str(junk), "local-abc")
    assert t.poll() is False and t.view(False) == [] and t.view(True) == []
    missing = SessionTail(str(tmp_path / "absent" / "state.db"), "local-abc")
    assert missing.poll() is False and missing.view(False) == []
    empty = SessionTail(make_store(tmp_path / "empty.db"), "other-session")
    assert empty.poll() is False and empty.view(False) == []
    mon = Monitor(clock=Clock(), hermes_home=str(tmp_path),
                  collect=lambda: snap([dict(run_row("b", "t_1"), session_id="local-abc")]))
    mon.refresh()
    mon.poll_logs()                                         # the poll above, through the app
    assert "waiting for output" in render_text(mon, width=120, height=12)


def test_the_display_set_is_hermes_own(tmp_path):
    """``active=1 OR compacted=1``, model-only rows excluded, non-JSON metadata kept."""
    t = live_session_tail(tmp_path, rows=[
        dict(role="assistant", content="model-only, never shown",
             display_metadata='{"model_only": true}'),
        dict(role="user", content="dropped by compression", active=0, compacted=0),
        dict(role="user", content="kept as a summary", active=0, compacted=1),
        dict(role="assistant", content="metadata is not json", display_metadata="oops"),
    ])
    t.poll()
    assert t.view(False) == ["▸ kept as a summary", "metadata is not json"]


def test_a_tool_result_does_not_repeat_its_call_line(tmp_path):
    t = live_session_tail(tmp_path, rows=[
        dict(role="assistant", content="", tool_calls=[call("read_file", {"path": "a.py"}, "c1")]),
        dict(role="tool", content="print(1)", tool_name="read_file", tool_call_id="c1"),
    ])
    t.poll()
    assert t.view(False) == ["┊ read_file  a.py"]


def test_a_result_whose_call_was_never_announced_still_gets_its_line(tmp_path):
    t = live_session_tail(tmp_path, rows=[
        dict(role="tool", content="...", tool_name="terminal", tool_call_id="c9"),
    ])
    t.poll()
    assert t.view(False) == ["┊ terminal"]


def test_structured_content_shows_its_text_parts(tmp_path):
    parts = json.dumps(["here is the screenshot", {"type": "image", "url": "x"},
                        {"type": "text", "text": "and the chart"}])
    assert render_rows([{"role": "assistant", "content": "\x00json:" + parts,
                         "tool_calls": None, "tool_call_id": None, "tool_name": None}]) \
        == ["here is the screenshot", "and the chart"]


# ── which source a panel reads ─────────────────────────────────────────────

def test_the_run_session_selects_the_store(tmp_path):
    row = dict(run_row("b", "t_1"), session_id="local-x")
    t = make_tail(row, str(tmp_path), "b", "t_1")
    assert isinstance(t, SessionTail) and t.source == "session"
    assert t.path == str(tmp_path / "profiles/backend-engineer/state.db")
    # the owner named the db in its own owner_admitted payload -> it wins
    row = dict(row, session_db=str(tmp_path / "owner" / "state.db"))
    assert make_tail(row, str(tmp_path), "b", "t_1").path == row["session_db"]
    # no session id at all -> the worker log, labelled as the fallback
    row = {k: v for k, v in row.items() if k != "session_id"}
    log = make_tail(row, str(tmp_path), "b", "t_1")
    assert isinstance(log, LogTail) and log.source == "log"
    assert log.path == log_path("b", "t_1", str(tmp_path))


def test_the_header_marks_which_source_is_being_read(tmp_path):
    rows = [run_row("b", "t_1")]
    write_log(tmp_path, "b", "t_1", "Session: local-abc\n")
    mon = Monitor(clock=Clock(), hermes_home=str(tmp_path), collect=lambda: snap(rows))
    mon.refresh()
    mon.poll_logs()
    out = render_text(mon, width=160, height=14)
    assert "src log (fallback)" in out and "Session: local-abc" in out
    rows[0] = dict(rows[0], session_id="local-abc")
    mon.refresh()
    mon.poll_logs()
    assert "src session" in render_text(mon, width=160, height=14)


def test_a_panel_switches_source_cleanly_when_the_owner_admits_the_run(tmp_path):
    """The owner admits a run ~2 s after the claim; the panel must not mix sources."""
    write_log(tmp_path, "b", "t_1", "client stderr only\n")
    make_store(tmp_path / "profiles/backend-engineer/state.db", "local-abc",
               [dict(role="assistant", content="real worker text")])
    rows = [run_row("b", "t_1")]
    mon = Monitor(clock=Clock(), hermes_home=str(tmp_path), collect=lambda: snap(rows))
    mon.refresh()
    mon.poll_logs()
    assert isinstance(mon.tails[("b", "t_1")], LogTail)
    assert "client stderr only" in render_text(mon, width=160, height=14)
    rows[0] = dict(rows[0], session_id="local-abc")
    mon.refresh()
    mon.poll_logs()
    tail = mon.tails[("b", "t_1")]
    assert isinstance(tail, SessionTail) and tail.view(False) == ["real worker text"]
    out = render_text(mon, width=160, height=14)
    assert "real worker text" in out and "client stderr only" not in out
    assert mon.order == [("b", "t_1")]                      # one panel, not two
