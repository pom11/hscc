"""No-ANSI regression for hscc kanban stale (hscc_daemon kanban_cli).

Every human view converted to render through ``hscc_daemon.cli_theme`` must
emit NO ANSI escape codes when stdout is not a tty (piped / captured) — Rich's
Console handles this automatically, and these tests pin it per surface so a
regression (a stray ``print``/forced colour) is caught in CI.

The ``--json`` machine paths are a separate hard invariant (byte-identical,
no reformat); they are asserted in ``test_kanban_cli.py`` itself (the JSON
must remain parseable), so only the human (non-json) paths are tested here for
ANSI absence.

Boards are fake in-memory sqlite (reuse the _FakeKb helper style), never the
operator's live boards.
"""

import datetime
import io
import json
from contextlib import redirect_stdout

import pytest

import hscc_daemon.autodown as ad
from hscc_daemon.kanban_cli import cmd_kanban


def _no_ansi(fn):
    """Run ``fn()`` with stdout captured to a non-tty StringIO and assert the
    captured output contains no ANSI escape sequence."""
    f = io.StringIO()
    with redirect_stdout(f):
        rc = fn()
    out = f.getvalue()
    assert "\x1b[" not in out, f"ANSI escape found in piped output: {out!r}"
    return out, rc


def _t(id, status, age_days, title="t"):
    """Build a task row for the fake lib: created_at epoch = now - age_days."""
    now = datetime.datetime.now(datetime.timezone.utc)
    created = int(now.timestamp()) - age_days * 86400
    return (id, title, "w", status, created)


class _FakeKb:
    """Fake Hermes kanban lib over real in-memory sqlite boards (see
    test_kanban_cli.py for the full shape)."""

    def __init__(self, boards=None):
        import sqlite3
        self._conns = {}
        for slug, rows in (boards or {"default": []}).items():
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            conn.execute(
                "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, "
                "assignee TEXT, status TEXT, created_at INTEGER)")
            for (tid, title, assignee, status, created) in rows:
                conn.execute(
                    "INSERT INTO tasks (id, title, assignee, status, created_at) "
                    "VALUES (?,?,?,?,?)", (tid, title, assignee, status, created))
            conn.commit()
            self._conns[slug] = conn

    def list_boards(self):
        return [{"slug": slug} for slug in self._conns]

    def connect_closing(self, board=None):
        return _ConnCM(self._conns.get(board) or self._conns.get("default"))


class _ConnCM:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *exc):
        return False


def _patch_kb(monkeypatch, boards):
    monkeypatch.setattr(ad, "_load_kanban_db_or_default", lambda: _FakeKb(boards))


class TestNoAnsiStale:
    def test_empty(self, monkeypatch):
        _patch_kb(monkeypatch, {"default": []})
        _no_ansi(lambda: cmd_kanban(["stale"]))

    def test_with_tasks(self, monkeypatch):
        _patch_kb(monkeypatch, {"hscc": [_t("t-1", "todo", 8)]})
        _no_ansi(lambda: cmd_kanban(["stale"]))

    def test_archive(self, monkeypatch):
        _patch_kb(monkeypatch, {"hscc": [_t("t-1", "todo", 8)]})
        _no_ansi(lambda: cmd_kanban(["stale", "--archive", "t-1"]))

    def test_help(self, monkeypatch):
        _patch_kb(monkeypatch, {"default": []})
        _no_ansi(lambda: cmd_kanban([]))
