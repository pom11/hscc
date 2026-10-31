"""Tests for hscc_daemon/cards_live.py — `hscc cards monitor`.

Offline only: a fake collector, a fake clock and worker logs under tmp_path.
NEVER the operator's live boards or ~/.hermes.
"""

import io

import pytest

from hscc_daemon import cards_live, kanban_live
from hscc_daemon.cli_theme import make_console
from hscc_daemon.cards_live import (LogTail, Monitor, ViewState, clean_line, cmd_cards,
                                    fit_header, grid_shape, is_tool_line, log_path,
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


def snap(running=(), ready=(), events=(), cap=10):
    return {"running": list(running), "ready": list(ready), "events": list(events),
            "caps": (cap, None), "errors": [], "boards": [{"name": "b"}]}


def write_log(home, board, tid, text, mode="w"):
    p = log_path(board, tid, str(home))
    import os
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, mode, encoding="utf-8") as f:
        f.write(text)
    return p


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
