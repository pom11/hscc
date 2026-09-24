"""No-ANSI regression for hscc kanban blocked (hscc_daemon kanban_blocked).

Every human view converted to render through ``hscc_daemon.cli_theme`` must
emit NO ANSI escape codes when stdout is not a tty (piped / captured). The
``--json`` paths are asserted parseable in ``test_kanban_blocked.py``; here we
pin only the human (non-json) surfaces. Boards are fake in-memory sqlite,
never the operator's live boards.
"""

import io
from contextlib import redirect_stdout

import pytest

import hscc_daemon.autodown as ad
from hscc_daemon.kanban_blocked import cmd_kanban, cmd_blocked


def _no_ansi(fn):
    """Run ``fn()`` with stdout captured to a non-tty StringIO and assert the
    captured output contains no ANSI escape sequence."""
    f = io.StringIO()
    with redirect_stdout(f):
        rc = fn()
    out = f.getvalue()
    assert "\x1b[" not in out, f"ANSI escape found in piped output: {out!r}"
    return out, rc


def _fake_blocked(*ids):
    """Build a fake kanban lib whose SELECT returns blocked rows. ``ids`` are
    the blocked task ids (each with why ``(no block reason recorded)``)."""
    import sqlite3

    class _Conn:
        def execute(self, sql, *a):
            class Cur:
                def fetchall(_self):
                    return [
                        {"id": tid, "status": "blocked", "assignee": "w",
                         "created_at": 0, "block_kind": None,
                         "last_failure_error": None, "title": "t", "keys": []}
                        for tid in ids
                    ]
            return Cur()

    class _CM:
        def __enter__(self):
            return _Conn()
        def __exit__(self, *exc):
            return False

    class _Kb:
        def list_boards(self):
            return [{"slug": "hscc"}]
        def connect_closing(self, board=None):
            return _CM()

    return _Kb()


class TestNoAnsiBlocked:
    def test_empty(self, monkeypatch):
        monkeypatch.setattr(ad, "_load_kanban_db_or_default",
                            lambda: _fake_blocked(), raising=False)
        _no_ansi(lambda: cmd_blocked([], False))

    def test_with_tasks(self, monkeypatch):
        monkeypatch.setattr(ad, "_load_kanban_db_or_default",
                            lambda: _fake_blocked("c1"), raising=False)
        _no_ansi(lambda: cmd_blocked([], False))

    def test_recover(self, monkeypatch):
        monkeypatch.setattr(ad, "_load_kanban_db_or_default",
                            lambda: _fake_blocked("c1"), raising=False)
        _no_ansi(lambda: cmd_blocked(["--recover", "c1"], False))

    def test_help(self, monkeypatch):
        _no_ansi(lambda: cmd_kanban([]))
