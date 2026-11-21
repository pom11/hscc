"""Parity: every widget card/kanban route answers from the CLI's own functions.

One fixture board set is read twice — once through the CLI verbs
(``hscc cards monitor`` / ``hscc kanban monitor|blocked|stale``) and once over
the real HTTP routes — and the numbers/rows must be equal. A second, UNREGISTERED
board is present in the fake Hermes lib and must appear in neither answer.

Both reads share ONE pinned instant (`pinned_collect_clock`): `age_s` is derived
from `collect()`'s single clock sample, and the two doors sample it
independently, so an unpinned clock makes the exact-equality assertions a race.
"""

import contextlib
import json
import sqlite3
import threading
import types

import pytest

import api_server
import routes_kanban
import routes_project

from hscc_daemon import autodown, cards_live, kanban_blocked, kanban_cli, kanban_live

NOW = 1_800_000_000

SCHEMA = """
CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, assignee TEXT,
  status TEXT, priority INTEGER, created_by TEXT, created_at INTEGER,
  started_at INTEGER, completed_at INTEGER, workspace_kind TEXT,
  workspace_path TEXT, branch_name TEXT, project_id INTEGER, claim_lock TEXT,
  claim_expires INTEGER, tenant TEXT, result TEXT, idempotency_key TEXT,
  consecutive_failures INTEGER, worker_pid INTEGER, last_failure_error TEXT,
  max_runtime_seconds INTEGER, last_heartbeat_at INTEGER, current_run_id INTEGER,
  workflow_template_id INTEGER, current_step_key TEXT, skills TEXT,
  model_override TEXT, provider_override TEXT, max_retries INTEGER,
  goal_mode INTEGER, goal_max_turns INTEGER, session_id TEXT, block_kind TEXT,
  block_recurrences INTEGER, reasoning_effort TEXT, completion_contract TEXT,
  worker_started_at INTEGER);
CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT,
  step_key TEXT, status TEXT, claim_lock TEXT, claim_expires INTEGER,
  worker_pid INTEGER, max_runtime_seconds INTEGER, last_heartbeat_at INTEGER,
  started_at INTEGER, ended_at INTEGER, outcome TEXT, summary TEXT,
  metadata TEXT, error TEXT, worker_started_at INTEGER);
CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
  run_id INTEGER, kind TEXT, payload TEXT, created_at INTEGER);
CREATE TABLE task_comments (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
  author TEXT, body TEXT, created_at INTEGER);
"""

_TASK_COLS = ("id", "title", "assignee", "status", "priority", "created_at",
              "completed_at", "last_heartbeat_at", "current_run_id",
              "block_kind", "last_failure_error", "consecutive_failures")


def _task(conn, tid, status, *, title=None, assignee="backend-engineer",
          created_at=None, completed_at=None, current_run_id=None,
          block_kind=None):
    values = (tid, title or f"card {tid}", assignee, status, 0, created_at,
              completed_at, created_at, current_run_id, block_kind, None, 0)
    conn.execute(
        f"INSERT INTO tasks ({','.join(_TASK_COLS)}) VALUES "
        f"({','.join('?' * len(_TASK_COLS))})", values)


class _Ctx:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *a):
        return False


class _FakeKb:
    def __init__(self):
        self.conns = {}
        for slug, build in (("hscc", self._build_hscc),
                            ("junk-unregistered", self._build_junk)):
            # The API server answers on a worker thread; these boards are read
            # from that thread while the fixture built them on the test thread.
            conn = sqlite3.connect(":memory:", check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.executescript(SCHEMA)
            build(conn)
            conn.commit()
            self.conns[slug] = conn

    @staticmethod
    def _build_hscc(conn):
        conn.execute(
            "INSERT INTO task_runs (id, task_id, profile, status, started_at, "
            "last_heartbeat_at) VALUES (1, 't_run', 'backend-engineer', "
            "'running', ?, ?)", (NOW - 600, NOW - 30))
        _task(conn, "t_run", "running", title="run the fixture",
              created_at=NOW - 9000, current_run_id=1)
        _task(conn, "t_ready", "ready", title="waiting card",
              created_at=NOW - 7200)
        _task(conn, "t_todo", "todo", title="todo card", created_at=NOW - 500)
        _task(conn, "t_blocked", "blocked", title="stuck card",
              created_at=NOW - 2 * 86400, block_kind="needs_input")
        _task(conn, "t_done", "done", title="finished card",
              created_at=NOW - 3 * 86400, completed_at=NOW - 600)

    @staticmethod
    def _build_junk(conn):
        _task(conn, "t_junk_run", "running", title="orphan running",
              created_at=NOW - 60)
        _task(conn, "t_junk_blocked", "blocked", title="orphan blocked",
              created_at=NOW - 60, block_kind="needs_input")

    def list_boards(self):
        return [{"slug": slug} for slug in self.conns]

    @contextlib.contextmanager
    def connect_closing(self, board=None):
        yield self.conns[board]


@pytest.fixture(autouse=True)
def pinned_collect_clock(monkeypatch):
    """ONE pinned instant for every ``collect()`` pass in the process.

    ``age_s`` is ``collect()``'s ``now_s - created_at``, and ``now_s`` is sampled
    per call as ``int(time.time())`` (kanban_live.py:378). Both sides of every
    parity assertion here read the SAME code through two different doors — the
    CLI verb in-process, the route on the API server's worker thread — so each
    door samples the wall clock independently, milliseconds apart. When a
    whole-second boundary falls between the two samples, the two ``int()``
    truncations differ by one and the exact-equality assertion fails
    (measured: 4 failing runs in 51 solo runs of this file at 4764ff85).

    ``collect()`` already takes ``now=`` — the seam ``cards_live.Monitor`` and
    ``test_mobile_summary_equals_collect_board_view`` use — but nothing on the
    CLI or the route path passes it, so the pin goes at the module attribute
    both paths look up at call time. Only a MISSING ``now`` is filled: every
    other argument the callers pass reaches ``collect`` untouched, so a route
    that collected a different board scope than the CLI would still disagree.
    """
    real = kanban_live.collect

    def pinned(*args, **kwargs):
        kwargs.setdefault("now", NOW)
        return real(*args, **kwargs)

    monkeypatch.setattr(kanban_live, "collect", pinned)


@pytest.fixture
def kb(monkeypatch, tmp_path):
    fake = _FakeKb()
    monkeypatch.setattr(autodown, "_load_kanban_db_or_default", lambda: fake)
    monkeypatch.setattr(autodown, "registered_board_names",
                        lambda registry_path=None: ({"hscc"}, None))
    monkeypatch.setattr(kanban_live, "HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setattr(kanban_live, "LEDGER_PATH", str(tmp_path / "ledger"))
    return fake


@pytest.fixture
def running(tmp_path, kb):
    srv = types.SimpleNamespace()
    srv.server = api_server.create_server(hscc_dir=str(tmp_path / "hscc"),
                                          addr=("127.0.0.1", 0))
    srv.host, srv.port = srv.server.server_address[:2]
    thread = threading.Thread(target=srv.server.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.server.shutdown()
    srv.server.server_close()


@pytest.fixture
def token(running):
    return api_server.load_token(running.server.ctx.hscc_dir)


def _get(running, token, path):
    import http.client
    conn = http.client.HTTPConnection(running.host, running.port, timeout=8)
    conn.request("GET", path, headers={"Authorization": "Bearer " + token})
    resp = conn.getresponse()
    payload = json.loads(resp.read() or b"{}")
    conn.close()
    assert resp.status == 200, payload
    return payload


def _cli_json(capsys, argv):
    assert kanban_cli.cmd_kanban(argv) == 0
    return json.loads(capsys.readouterr().out)


def test_cards_route_equals_cards_monitor(running, token, kb):
    mon = cards_live.Monitor(kanban_db=kb, clock=lambda: NOW)
    mon.refresh()
    expected = mon.data["running"] + mon.data["ready"]
    payload = _get(running, token, "/v1/cards")
    assert payload["count"] == len(expected) == 2
    got = {row["id"]: row for row in payload["cards"]}
    for row in expected:
        assert row["id"] in got
        assert got[row["id"]]["title"] == row["title"]
        assert got[row["id"]]["board"] == row["board"] == "hscc"
        assert got[row["id"]]["status"] == row["status"]
        assert got[row["id"]]["age_s"] == row["age_s"]
    assert not any(row["id"].startswith("t_junk") for row in payload["cards"])


def test_running_route_equals_kanban_monitor(running, token, kb, capsys):
    cli = _cli_json(capsys, ["monitor", "--json"])
    payload = _get(running, token, "/v1/kanban/running")
    assert payload["count"] == cli["running"].__len__() == 1
    assert [r["id"] for r in payload["tasks"]] == [
        r["id"] for r in cli["running"]]
    assert payload["tasks"][0]["age_s"] == cli["running"][0]["age_s"]
    assert "junk-unregistered" not in payload["boards"]


class _SteppingClock:
    """A ``time`` stand-in whose ``time()`` jumps past whole-second marks.

    Only ``time()`` is needed on this path — the one other ``time.*`` use inside
    ``kanban_live`` is ``run_live``'s ``sleep``, which no test here enters.
    """

    def __init__(self, start, step):
        self.t = start
        self.step = step

    def time(self):
        value, self.t = self.t, self.t + self.step
        return value


def test_running_route_parity_survives_a_clock_step(running, token, kb, capsys,
                                                    monkeypatch):
    """The parity assertion holds even when the wall clock crosses a second.

    The flake this pins (t_0e457f72) is a ~1 in 10 race, so the regression test
    cannot wait for it: it replaces ``kanban_live``'s clock with one that steps
    1.5 s per call, so the CLI's ``collect()`` pass and the route's can never
    land in the same second. Without the pinned instant in
    ``pinned_collect_clock`` the two sides of
    ``test_running_route_equals_kanban_monitor``'s exact-equality assertion land
    seconds apart and it goes red — that red is the proof the pin is
    load-bearing (verified at 4764ff85, the pre-fix sha).
    """
    monkeypatch.setattr(kanban_live, "time",
                        _SteppingClock(1_800_000_000.9, 1.5))
    cli = _cli_json(capsys, ["monitor", "--json"])
    payload = _get(running, token, "/v1/kanban/running")
    assert cli["running"][0]["age_s"] == payload["tasks"][0]["age_s"] == 9000


def test_blocked_route_equals_kanban_blocked(running, token, kb, capsys):
    assert kanban_blocked.cmd_kanban(["blocked", "--json"]) == 0
    cli = json.loads(capsys.readouterr().out)
    payload = _get(running, token, "/v1/kanban/blocked")
    assert [t["id"] for t in payload["tasks"]] == [
        t["id"] for t in cli["tasks"]] == ["t_blocked"]
    assert payload["count"] == len(cli["tasks"]) == 1


def test_stale_route_equals_kanban_stale(running, token, kb, capsys):
    cli = _cli_json(capsys, ["stale", "--json", "--older-than", "0"])
    payload = _get(running, token, "/v1/kanban/stale?older_than=0")
    cli_ids = sorted(t["id"] for t in cli["tasks"])
    route_ids = sorted(t["id"] for t in payload["tasks"])
    # `kanban stale` lists the non-terminal work queue; blocked cards have
    # their own verb/route, so neither side folds them in.
    assert route_ids == cli_ids == ["t_ready", "t_run", "t_todo"]
    assert payload["count"] == len(cli["tasks"])


def test_mobile_summary_equals_collect_board_view(running, token, kb):
    data = kanban_live.collect(kanban_db=kb, now=NOW, registered_only=True,
                               read_caps=lambda: (None, None))
    expected = {
        b["name"]: kanban_live.board_status_counts(b) for b in data["boards"]}
    payload = _get(running, token, "/v1/mobile/summary")
    got = {b["board"]: b for b in payload["boards"]}
    assert set(got) == set(expected) == {"hscc"}
    for board, want in expected.items():
        assert got[board]["counts"] == want["counts"]
        assert got[board]["total"] == want["total"]
    assert got["hscc"]["counts"] == {
        "todo": 1, "ready": 1, "running": 1, "blocked": 1,
        "done": 1, "archived": 0,
    }
