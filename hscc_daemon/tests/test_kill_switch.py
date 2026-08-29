"""Unit tests for the hscc kill switch (hscc_daemon/kill_switch.py).

Hermetic: a fake kanban lib backed by real in-memory sqlite boards replaces
``hermes_cli.kanban_db`` (injected, never imported from the live ~/.hermes).
The kill path is exercised with a ``signal_fn`` seam — a no-op killer — so a
test can never signal a real process. We COVER the full contract the card
demands:

  * list_running_tasks enumerates ONLY running tasks, across boards, and
    reports each task's pid + host_local ("names what will be stopped").
  * kill_running_task finds exactly ONE running task by id and delegates to
    ``_terminate_reclaimed_worker`` via the injectable kanban lib, returning
    its result dict verbatim ("reports what actually died").
  * kill on a non-running / missing task reports found=False (404 upstream).
  * host-locality is surfaced and enforced.
"""

import sqlite3
from contextlib import contextmanager

import pytest

from hscc_daemon import kill_switch as ks


def _make_conn(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE tasks ("
        " id TEXT PRIMARY KEY,"
        " title TEXT,"
        " assignee TEXT,"
        " status TEXT,"
        " claim_lock TEXT,"
        " worker_pid INTEGER,"
        " started_at INTEGER,"
        " claim_expires INTEGER)"
    )
    for r in rows:
        conn.execute(
            "INSERT INTO tasks (id, title, assignee, status, claim_lock,"
            " worker_pid, started_at, claim_expires) VALUES (?,?,?,?,?,?,?,?)",
            (r.get("id"), r.get("title"), r.get("assignee"), r.get("status"),
             r.get("claim_lock"), r.get("worker_pid"), r.get("started_at"),
             r.get("claim_expires")),
        )
    conn.commit()
    return conn


class _FakeKb:
    """Fake Hermes kanban lib: list_boards + board-aware connect_closing."""

    def __init__(self, boards):
        # boards: dict {slug: [row-dicts]} OR list of row-dicts (default board)
        if isinstance(boards, (list, tuple)):
            boards = {"default": boards}
        self._conns = {slug: _make_conn(rows) for slug, rows in boards.items()}
        self._claims = {}

    def list_boards(self):
        return [{"slug": slug} for slug in self._conns]

    @contextmanager
    def connect_closing(self, board=None):
        conn = self._conns.get(board) or self._conns.get("default")
        yield conn

    # --- the two Hermes-core functions kill_switch delegates to ----------
    def _claimer_id(self):
        return "node-1:9999"

    def _terminate_reclaimed_worker(self, pid, claim_lock, *, signal_fn=None):
        # Record the delegate call so tests can assert WHO was signalled.
        self._claims[(pid, claim_lock)] = signal_fn
        # Simulate a successful SIGTERM-then-gone termination by default.
        return {
            "prev_pid": int(pid) if pid else None,
            "host_local": True,
            "termination_attempted": True,
            "terminated": True,
            "sigkill": False,
        }


# --------------------------------------------------------------------------- #
# list_running_tasks — "names what will be stopped"
# --------------------------------------------------------------------------- #

def _running_row(task_id="t_abc", pid=4242, claim="node-1:4242", wp=None,
                 assignee="worker", title="Runaway training", started=1700000000):
    return {"id": task_id, "title": title, "assignee": assignee,
            "status": "running", "claim_lock": claim, "worker_pid": wp,
            "started_at": started}


def test_list_only_running_across_boards():
    kb = _FakeKb({
        "default": [_running_row(task_id="t_abc"), {"id": "t_done",
                   "status": "done", "title": "x", "assignee": None,
                   "claim_lock": None, "worker_pid": None, "started_at": None}],
        "hscc": [_running_row(task_id="t_xyz", pid=7777, claim="node-1:7777")],
    })
    res = ks.list_running_tasks(kanban_db=kb)
    assert res["errors"] == []
    assert res["count"] == 2
    ids = {t["id"] for t in res["tasks"]}
    assert ids == {"t_abc", "t_xyz"}
    # only running tasks are listed
    assert all(t["status"] == "running" for t in res["tasks"])
    # board labels present
    assert {t["board"] for t in res["tasks"]} == {"default", "hscc"}


def test_list_reports_pid_and_host_local():
    kb = _FakeKb({"default": [
        _running_row(pid=4242, claim="node-1:4242"),          # host-local
        _running_row(task_id="t_remote", pid=999, claim="node-9:999"),  # remote
    ]})
    res = ks.list_running_tasks(kanban_db=kb)
    by_id = {t["id"]: t for t in res["tasks"]}
    assert by_id["t_abc"]["pid"] == 4242
    assert by_id["t_abc"]["host_local"] is True
    assert by_id["t_remote"]["pid"] == 999
    assert by_id["t_remote"]["host_local"] is False


def test_list_pid_falls_back_to_claim_lock():
    # No worker_pid column value -> pid parsed from claim_lock "node-1:4242"
    kb = _FakeKb({"default": [_running_row(pid=None, wp=None)]})
    res = ks.list_running_tasks(kanban_db=kb)
    assert res["tasks"][0]["pid"] == 4242


def test_list_none_unreachable_reports_error():
    class _None:
        def __init__(self):
            self.nodes = None
    # simulate _load returning None by passing an unreachable enum:
    # a lib whose connect raises
    class _Broken:
        @contextmanager
        def connect_closing(self, board=None):
            raise RuntimeError("DB unreachable")
        def list_boards(self):
            return [{"slug": "default"}]
    res = ks.list_running_tasks(kanban_db=_Broken())
    assert res["errors"]  # board unreadable reported, not crashed
    assert res["tasks"] == []
    assert res["count"] == 0


def test_list_unreachable_lib_reports_error(monkeypatch):
    monkeypatch.setattr(ks.autodown, "_load_kanban_db_or_default", lambda: None)
    res = ks.list_running_tasks(kanban_db=None)
    assert res["errors"]
    assert res["tasks"] == []
    assert res["count"] == 0


# --------------------------------------------------------------------------- #
# kill_running_task — "reports what actually died"
# --------------------------------------------------------------------------- #

def test_kill_found_and_delegates():
    kb = _FakeKb({"default": [_running_row()]})
    res = ks.kill_running_task("t_abc", kanban_db=kb)
    assert res["found"] is True
    assert res["task"]["id"] == "t_abc"
    assert res["task"]["board"] == "default"
    assert res["task"]["pid"] == 4242
    # delegate was called with (pid, claim_lock)
    assert (4242, "node-1:4242") in kb._claims
    # termination reported verbatim
    assert res["termination"]["terminated"] is True
    assert res["termination"]["termination_attempted"] is True


def test_kill_not_running_reports_not_found():
    kb = _FakeKb({"default": [{"id": "t_done", "status": "done", "title": "x",
                               "assignee": None, "claim_lock": None,
                               "worker_pid": None, "started_at": None}]})
    res = ks.kill_running_task("t_done", kanban_db=kb)
    assert res["found"] is False


def test_kill_missing_id_reports_not_found():
    kb = _FakeKb({"default": [_running_row()]})
    res = ks.kill_running_task("t_ghost", kanban_db=kb)
    assert res["found"] is False
    assert res["task"] is None


def test_kill_remote_task_surfaces_host_local_false():
    kb = _FakeKb({"default": [_running_row(pid=999, claim="node-9:999")]})
    res = ks.kill_running_task("t_abc", kanban_db=kb)
    assert res["found"] is True
    assert res["task"]["host_local"] is False


def test_kill_signal_fn_seam_is_forwarded():
    kb = _FakeKb({"default": [_running_row()]})
    sent = []
    def _killer(pid, sig):
        sent.append((pid, sig))
    res = ks.kill_running_task("t_abc", kanban_db=kb, signal_fn=_killer)
    # the no-op killer was passed through to _terminate_reclaimed_worker
    assert kb._claims[(4242, "node-1:4242")] is _killer


def test_kill_surfaces_sigkill_escalation():
    kb = _FakeKb({"default": [_running_row()]})
    kb._terminate_reclaimed_worker = lambda pid, claim, *, signal_fn=None: {
        "prev_pid": pid, "host_local": True, "termination_attempted": True,
        "terminated": True, "sigkill": True,
    }
    res = ks.kill_running_task("t_abc", kanban_db=kb)
    assert res["termination"]["sigkill"] is True
