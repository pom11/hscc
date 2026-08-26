"""Tests for hscc_daemon.kanban_blocked — SHOW why a card is blocked + recover it.

Card t_ab177036. Hermes' ``reclaim_task`` cannot recover a ``blocked`` card
(early guard at hermes_cli/kanban_db.py:4491-4493 returns False for a
non-running task with no claim_lock), so this module provides a working
recovery path (blocked → ready) plus a SHOW-why listing. These tests use a
fake kanban lib backed by in-memory sqlite boards (never the operator's live
~/.hermes), mirroring the pattern in test_autodown.py::_FakeKb but with the
richer task schema this feature reads/writes.

Important: recovery is never automatic and never bulk. Every test here uses an
EXPLICIT task id; there is no path that flips a card without one.
"""

import contextlib
import datetime
import json
import sqlite3
import time

import pytest

from hscc_daemon import kanban_blocked as kbx
from hscc_daemon import autodown


_TASK_COLS = (
    "id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT, "
    "created_at INTEGER, block_kind TEXT, last_failure_error TEXT, "
    "consecutive_failures INTEGER, claim_lock TEXT, claim_expires INTEGER, "
    "worker_pid INTEGER"
)


class _FakeKb:
    """Fake Hermes kanban library backed by real in-memory sqlite boards.

    Supports a ``{slug: [task_dicts...]}`` shape (each task dict may carry any
    of the task columns; defaults applied) so tests can build blocked and
    non-blocked cards across multiple boards. Exposes ``list_boards()`` +
    ``connect_closing(board=...)`` exactly like autodown's enumeration seam.
    """

    def __init__(self, boards=None):
        self._conns = {}
        self.opened = []
        if boards is None:
            boards = {"default": []}
        for slug, tasks in boards.items():
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            conn.execute(f"CREATE TABLE tasks ({_TASK_COLS})")
            conn.execute(
                "CREATE TABLE task_comments (id INTEGER PRIMARY KEY "
                "AUTOINCREMENT, task_id TEXT, author TEXT, body TEXT, "
                "created_at INTEGER)"
            )
            conn.execute(
                "CREATE TABLE task_events (id INTEGER PRIMARY KEY "
                "AUTOINCREMENT, task_id TEXT, run_id INTEGER, kind TEXT, "
                "payload TEXT, created_at INTEGER)"
            )
            for i, spec in enumerate(tasks):
                defaults = {
                    "title": "",
                    "assignee": None,
                    "status": "blocked",
                    "created_at": int(time.time()),
                    "block_kind": None,
                    "last_failure_error": None,
                    "consecutive_failures": 0,
                }
                defaults.update(spec)
                tid = defaults["id"] or f"{slug}-{i}"
                conn.execute(
                    "INSERT INTO tasks (id, title, assignee, status, "
                    "created_at, block_kind, last_failure_error, "
                    "consecutive_failures, claim_lock, claim_expires, "
                    "worker_pid) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (tid, defaults["title"], defaults["assignee"],
                     defaults["status"], defaults["created_at"],
                     defaults["block_kind"], defaults["last_failure_error"],
                     defaults["consecutive_failures"], None, None, None),
                )
            conn.commit()
            self._conns[slug] = conn

    def list_boards(self):
        return [{"slug": slug} for slug in self._conns]

    @contextlib.contextmanager
    def connect_closing(self, board=None):
        self.opened.append(board)
        conn = self._conns.get(board) or self._conns.get("default")
        yield conn


class _UnreachableKb:
    """kanban lib whose connect raises — exercises the fail-safe error path."""

    @contextlib.contextmanager
    def connect_closing(self, board=None):
        raise RuntimeError("DB unreachable")


def _old_ts(days_ago):
    return int(time.time()) - days_ago * 86400


def _assert_recovery_leaves_trail(kb, board, tid, reason):
    conn = kb._conns[board]
    events = conn.execute(
        "SELECT kind FROM task_events WHERE task_id=?", (tid,)).fetchall()
    assert any(e["kind"] == "recovered" for e in events)
    comments = conn.execute(
        "SELECT author, body FROM task_comments WHERE task_id=?", (tid,)).fetchall()
    assert any(c["author"] == "hscc-recover" for c in comments)
    if reason:
        assert any(reason in c["body"] for c in comments)


# ---------------------------------------------------------------------------
# list_blocked_tasks — SHOW why
# ---------------------------------------------------------------------------

class TestListBlocked:
    def test_list_only_blocked_across_multiple_boards(self):
        kb = _FakeKb({
            "hscc": [
                {"id": "b1", "status": "blocked", "block_kind": "capability",
                 "last_failure_error": "worker died loading model",
                 "created_at": _old_ts(3)},
                {"id": "r1", "status": "running"},
                {"id": "d1", "status": "done"},
            ],
            "ios-app": [
                {"id": "b2", "status": "blocked", "created_at": _old_ts(1)},
            ],
        })
        res = kbx.list_blocked_tasks(kb)
        ids = [t["id"] for t in res["tasks"]]
        assert "b1" in ids and "b2" in ids
        assert "r1" not in ids and "d1" not in ids
        for t in res["tasks"]:
            assert t["status"] == "blocked"

    def test_why_carries_block_kind_and_error(self):
        kb = _FakeKb({
            "default": [{
                "id": "x", "block_kind": "needs_input",
                "last_failure_error": "need the API key",
                "consecutive_failures": 3,
            }],
        })
        res = kbx.list_blocked_tasks(kb)
        t = res["tasks"][0]
        assert t["block_kind"] == "needs_input"
        assert "needs_input" in t["why"]
        assert "API key" in t["why"]
        assert "3 consecutive" in t["why"]

    def test_oldest_first(self):
        kb = _FakeKb({
            "default": [
                {"id": "new", "created_at": _old_ts(1)},
                {"id": "old", "created_at": _old_ts(30)},
            ],
        })
        ids = [t["id"] for t in kbx.list_blocked_tasks(kb)["tasks"]]
        assert ids == ["old", "new"]

    def test_aggregates_board_count(self):
        kb = _FakeKb({"a": [{"id": "1"}], "b": [{"id": "2"}]})
        assert kbx.list_blocked_tasks(kb)["boards"] == 2

    def test_unreadable_board_reported_not_crash(self):
        kb = _FakeKb({"ok": [{"id": "g", "status": "blocked"}]})
        # Break the second board by adding it after construction with no conn.
        kb._conns["broken"] = _BrokenConn()
        res = kbx.list_blocked_tasks(kb)
        assert any("unreadable" in e for e in res["errors"])
        assert [t["id"] for t in res["tasks"]] == ["g"]

    def test_kanban_unreachable_returns_error(self):
        res = kbx.list_blocked_tasks(_UnreachableKb())
        assert any("unreadable" in e for e in res["errors"])

    def test_reflects_why_no_reason_recorded(self):
        kb = _FakeKb({"default": [{"id": "z"}]})
        t = kbx.list_blocked_tasks(kb)["tasks"][0]
        assert "no block reason" in t["why"]


class _BrokenConn:
    def execute(self, *a, **k):
        raise RuntimeError("boom")

    def close(self):
        pass


# ---------------------------------------------------------------------------
# recover_blocked_task — working recovery path
# ---------------------------------------------------------------------------

class TestRecoverBlocked:
    def test_recovers_blocked_to_ready(self):
        kb = _FakeKb({"default": [{"id": "c1", "status": "blocked"}]})
        label, ok = kbx.recover_blocked_task("c1", reason="verified done", kanban_db=kb)
        assert ok is True and label == "default"
        status = kb._conns["default"].execute(
            "SELECT status FROM tasks WHERE id='c1'").fetchone()["status"]
        assert status == "ready"

    def test_recovery_leaves_durable_trail(self):
        kb = _FakeKb({"default": [{"id": "c1", "status": "blocked"}]})
        kbx.recover_blocked_task("c1", reason="operator verified", kanban_db=kb)
        _assert_recovery_leaves_trail(kb, "default", "c1", "operator verified")

    def test_unknown_id_returns_false(self):
        kb = _FakeKb({"default": [{"id": "c1", "status": "blocked"}]})
        label, ok = kbx.recover_blocked_task("nope", kanban_db=kb)
        assert ok is False and label is None
        # untouched
        status = kb._conns["default"].execute(
            "SELECT status FROM tasks WHERE id='c1'").fetchone()["status"]
        assert status == "blocked"

    def test_never_mutates_non_blocked(self):
        kb = _FakeKb({"default": [{"id": "run", "status": "running"},
                                  {"id": "done", "status": "done"}]})
        label, ok = kbx.recover_blocked_task("run", kanban_db=kb)
        assert ok is False
        assert kb._conns["default"].execute(
            "SELECT status FROM tasks WHERE id='run'").fetchone()["status"] == "running"
        label2, ok2 = kbx.recover_blocked_task("done", kanban_db=kb)
        assert ok2 is False
        assert kb._conns["default"].execute(
            "SELECT status FROM tasks WHERE id='done'").fetchone()["status"] == "done"

    def test_scans_all_boards_finds_target(self):
        kb = _FakeKb({
            "a": [{"id": "other", "status": "done"}],
            "b": [{"id": "target", "status": "blocked"}],
        })
        label, ok = kbx.recover_blocked_task("target", kanban_db=kb)
        assert ok is True and label == "b"

    def test_unreachable_kanban_fails_cleanly(self):
        label, ok = kbx.recover_blocked_task("x", kanban_db=_UnreachableKb())
        assert ok is False and label is None

    def test_resets_failure_counter(self):
        kb = _FakeKb({"default": [{
            "id": "c1", "status": "blocked", "consecutive_failures": 9,
        }]})
        kbx.recover_blocked_task("c1", kanban_db=kb)
        assert kb._conns["default"].execute(
            "SELECT consecutive_failures FROM tasks WHERE id='c1'"
        ).fetchone()["consecutive_failures"] == 0


# ---------------------------------------------------------------------------
# CLI wiring (cmd_blocked)
# ---------------------------------------------------------------------------

class TestCmdBlocked:
    """CLI wiring for ``cmd_blocked``. Uses monkeypatch so the default
    kanban-lib loader (autodown._load_kanban_db_or_default) is redirected to a
    fake for the duration of each test — the same trick tests use for every
    daemon-side default; never touches the real ~/.hermes."""

    def test_list_json_is_valid(self, capsys, monkeypatch):
        kb = _FakeKb({"default": [{"id": "c1", "status": "blocked",
                                   "title": "Ship feature"}]})
        monkeypatch.setattr(autodown, "_load_kanban_db_or_default",
                            lambda: kb, raising=False)
        rc = kbx.cmd_blocked([], True)
        out = capsys.readouterr().out
        data = json.loads(out)
        assert rc == 0
        assert data["tasks"][0]["id"] == "c1"
        assert data["tasks"][0]["title"] == "Ship feature"

    def test_recover_cli(self, capsys, monkeypatch):
        kb = _FakeKb({"default": [{"id": "c1", "status": "blocked"}]})
        monkeypatch.setattr(autodown, "_load_kanban_db_or_default",
                            lambda: kb, raising=False)
        rc = kbx.cmd_blocked(["--recover", "c1", "--reason", "done"], True)
        out = capsys.readouterr().out
        data = json.loads(out)
        assert rc == 0 and data["recovered"] == "c1"
        _assert_recovery_leaves_trail(kb, "default", "c1", "done")

    def test_recover_unknown_id_error(self, capsys, monkeypatch):
        kb = _FakeKb({"default": [{"id": "c1", "status": "blocked"}]})
        monkeypatch.setattr(autodown, "_load_kanban_db_or_default",
                            lambda: kb, raising=False)
        rc = kbx.cmd_blocked(["--recover", "nope"], False)
        err = capsys.readouterr().err
        assert rc == 1 and "not found" in err

    def test_recover_requires_one_id(self, capsys, monkeypatch):
        kb = _FakeKb({"default": [{"id": "c1", "status": "blocked"}]})
        monkeypatch.setattr(autodown, "_load_kanban_db_or_default",
                            lambda: kb, raising=False)
        rc = kbx.cmd_blocked(["--recover"], False)
        err = capsys.readouterr().err
        assert rc == 1 and "requires a task id" in err

    def test_no_blocked_message(self, capsys, monkeypatch):
        kb = _FakeKb({"default": []})
        monkeypatch.setattr(autodown, "_load_kanban_db_or_default",
                            lambda: kb, raising=False)
        rc = kbx.cmd_blocked([], False)
        out = capsys.readouterr().out
        assert rc == 0 and "no blocked cards" in out


# ---------------------------------------------------------------------------
# cmd_kanban — group dispatcher
# ---------------------------------------------------------------------------

class TestCmdKanban:
    """``hscc kanban`` group routing: ``blocked`` locally, ``stale`` delegated
    to t_e751e652's kanban_cli when present, unknown subcommands rejected."""

    def test_help(self, capsys):
        rc = kbx.cmd_kanban(["--help"])
        out = capsys.readouterr().out
        assert rc == 0 and "blocked" in out and "--recover" in out

    def test_no_args_shows_help(self, capsys):
        rc = kbx.cmd_kanban([])
        assert rc == 0

    def test_routes_blocked(self, capsys, monkeypatch):
        kb = _FakeKb({"default": [{"id": "c1", "status": "blocked"}]})
        monkeypatch.setattr(autodown, "_load_kanban_db_or_default",
                            lambda: kb, raising=False)
        rc = kbx.cmd_kanban(["blocked"])
        out = capsys.readouterr().out
        assert rc == 0 and "c1" in out and "no block reason" in out

    def test_routes_blocked_recover(self, capsys, monkeypatch):
        kb = _FakeKb({"default": [{"id": "c1", "status": "blocked"}]})
        monkeypatch.setattr(autodown, "_load_kanban_db_or_default",
                            lambda: kb, raising=False)
        rc = kbx.cmd_kanban(["blocked", "--recover", "c1", "--reason", "ok"])
        out = capsys.readouterr().out
        assert rc == 0 and "recovered c1" in out

    def test_unknown_subcommand_rejected(self, capsys):
        rc = kbx.cmd_kanban(["bogus"])
        err = capsys.readouterr().err
        assert rc == 1 and "unknown kanban subcommand" in err
