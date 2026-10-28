"""Tests for hscc_daemon/kanban_live.py — `hscc kanban monitor`.

Offline only: a fake Hermes kanban lib over real in-memory sqlite boards with
the columns the collector reads. NEVER the operator's live boards or ~/.hermes.
"""

import io
import json
import sqlite3


from hscc_daemon import kanban_live
from hscc_daemon.cli_theme import make_console
from hscc_daemon.kanban_cli import _parse_monitor_args, cmd_kanban

NOW = 1_800_000_000

SCHEMA = """
CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT,
  priority INTEGER DEFAULT 0, created_at INTEGER, completed_at INTEGER,
  last_heartbeat_at INTEGER, current_run_id INTEGER);
CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT,
  status TEXT, started_at INTEGER, last_heartbeat_at INTEGER);
CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
  run_id INTEGER, kind TEXT, payload TEXT, created_at INTEGER);
"""


class _Ctx:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *a):
        return False


class _FakeKb:
    def __init__(self, boards, broken=()):
        self.conns = {}
        self.broken = set(broken)
        for slug, build in boards.items():
            c = sqlite3.connect(":memory:")
            c.row_factory = sqlite3.Row
            c.executescript(SCHEMA)
            build(c)
            self.conns[slug] = c

    def list_boards(self):
        return [{"slug": s} for s in list(self.conns) + list(self.broken)]

    def connect_closing(self, board=None):
        if board in self.broken:
            raise RuntimeError("database is locked")
        return _Ctx(self.conns[board])


def _alpha(c):
    c.execute("INSERT INTO task_runs VALUES (1,'t_run','backend-engineer','running',?,?)",
              (NOW - 5 * 3600, NOW - 30))
    c.execute("INSERT INTO tasks (id,title,assignee,status,created_at,current_run_id) "
              "VALUES ('t_run','long job','backend-engineer','running',?,1)", (NOW - 9000,))
    c.execute("INSERT INTO tasks (id,title,assignee,status,priority,created_at) "
              "VALUES ('t_rdy','waits','qa','ready',5,?)", (NOW - 7200,))
    c.execute("INSERT INTO task_events (task_id,kind,created_at) VALUES ('t_rdy','promoted',?)",
              (NOW - 2400,))
    c.execute("INSERT INTO tasks (id,title,assignee,status,created_at,completed_at) "
              "VALUES ('t_done','fin','qa','done',?,?)", (NOW - 90000, NOW - 600))
    c.execute("INSERT INTO task_events (task_id,kind,created_at) VALUES ('t_done','completed',?)",
              (NOW - 600,))
    c.execute("INSERT INTO task_events (task_id,kind,created_at) VALUES ('t_done','heartbeat',?)",
              (NOW - 500,))


def _beta(c):
    c.execute("INSERT INTO tasks (id,title,assignee,status,created_at) "
              "VALUES ('t_blk','stuck [/bold]','ml','blocked',?)", (NOW - 100,))
    c.execute("INSERT INTO task_events (task_id,kind,created_at) VALUES ('t_blk','gave_up',?)",
              (NOW - 120,))
    c.execute("INSERT INTO task_events (task_id,kind,created_at) VALUES ('t_blk','crashed',?)",
              (NOW - 7200,))  # outside the last-hour window


def _kb(**kw):
    return _FakeKb({"alpha": _alpha, "beta": _beta}, **kw)


def _collect(**kw):
    return kanban_live.collect(kanban_db=kw.pop("kb", None) or _kb(), now=NOW,
                               read_caps=lambda: (6, 2), **kw)


def test_collect_running_ready_counts_and_recent_events():
    d = _collect()
    assert [b["name"] for b in d["boards"]] == ["alpha", "beta"]
    (run,) = d["running"]
    assert run["worker"] == "backend-engineer"
    assert run["run_s"] == 5 * 3600 and run["hb_s"] == 30
    (rdy,) = d["ready"]
    assert rdy["wait_s"] == 2400  # since its last event, not since creation
    alpha = d["boards"][0]
    assert alpha["counts"] == {"running": 1, "ready": 1, "done": 1}
    assert alpha["done_24h"] == 1
    # heartbeats are not outcomes; events older than an hour drop out
    assert [(e["id"], e["kind"]) for e in d["events"]] == [
        ("t_blk", "gave_up"), ("t_done", "completed")]
    assert d["caps"] == (6, 2)


def test_board_filter_and_unreadable_board_does_not_stop_the_pass():
    d = _collect(kb=_kb(broken=["gamma"]))
    assert any("gamma" in e and "unreadable" in e for e in d["errors"])
    assert len(d["boards"]) == 2
    d = _collect(board="beta")
    assert [b["name"] for b in d["boards"]] == ["beta"] and not d["running"]


def test_render_escapes_titles_and_flags_full_cap():
    d = _collect()
    d["caps"] = (1, 2)
    buf = io.StringIO()
    make_console(file=buf, width=160, color_system=None).print(kanban_live.render(d, snapshot=True))
    out = buf.getvalue()
    assert "slots in use 1/1" in out and "global cap full" in out
    assert "t_run" in out and "5h00m" in out and "stuck [/bold]" in out


def test_short_pane_drops_lower_sections_first():
    d = _collect()
    buf = io.StringIO()
    make_console(file=buf, width=160, color_system=None).print(
        kanban_live.render(d, height=12))
    out = buf.getvalue()
    assert "running (1)" in out and "ready, waiting" in out
    assert "last hour" not in out


def test_fmt_age():
    assert [kanban_live.fmt_age(x) for x in (None, 5, 125, 3700, 90000)] == [
        "-", "5s", "2m", "1h01m", "1d1h"]


def test_parse_monitor_args():
    assert _parse_monitor_args(["--live", "--interval", "3", "--board", "x"]) == (
        {"live": True, "interval": 3, "board": "x"}, None)
    assert _parse_monitor_args(["--interval", "0"])[1]
    assert _parse_monitor_args(["--board"])[1]
    assert "unknown argument" in _parse_monitor_args(["--nope"])[1]


def test_cli_json_snapshot_and_live_json_refused(monkeypatch, capsys):
    monkeypatch.setattr(kanban_live, "collect",
                        lambda board=None: {"boards": [], "running": [], "ready": [],
                                            "events": [], "caps": (6, 2), "errors": [],
                                            "at": NOW})
    assert cmd_kanban(["monitor", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["caps"] == [6, 2]
    assert cmd_kanban(["monitor", "--json", "--live"]) == 1


def test_cli_help_executes_nothing(monkeypatch, capsys):
    monkeypatch.setattr(kanban_live, "collect", lambda **k: (_ for _ in ()).throw(AssertionError))
    assert cmd_kanban(["monitor", "--help"]) == 0
    assert "hscc kanban monitor" in capsys.readouterr().out


def test_hscc_kanban_dispatcher_keeps_json_for_delegated_verbs(monkeypatch, capsys):
    # `hscc kanban` enters through kanban_blocked.cmd_kanban, which strips
    # --json before delegating stale/monitor to kanban_cli — the flag must be
    # handed back or the "JSON" output is the human table.
    from hscc_daemon import kanban_blocked
    monkeypatch.setattr(kanban_live, "collect",
                        lambda board=None: {"boards": [], "running": [], "ready": [],
                                            "events": [], "caps": (6, 2), "errors": [],
                                            "at": NOW})
    assert kanban_blocked.cmd_kanban(["monitor", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["caps"] == [6, 2]
