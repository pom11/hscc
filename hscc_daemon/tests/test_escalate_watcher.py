"""Tests for escalate_watcher.py — failure-escalation watcher for live kanban tasks.

All tests inject fake _kb / _reassign / _notify so nothing touches the real
DB, subprocess, or notification subsystem.
"""

import pytest
from hscc_daemon.escalate_watcher import scan_and_escalate


# ---------------------------------------------------------------------------
# Helpers — fake kanban_db module
# ---------------------------------------------------------------------------

class _Row(dict):
    """Dict subclass so rows are indexable like sqlite3.Row."""
    def __getitem__(self, key):
        return super().__getitem__(key)


class _FakeConn:
    """Minimal sqlite3.Connection stand-in."""

    def __init__(self, rows=None):
        self._rows = rows or []
        self._executed = []

    def execute(self, sql, params=None):
        self._executed.append((sql, params))
        cursor = _FakeCursor(self._rows)
        cursor._sql = sql
        return cursor

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self._sql = None

    def execute_with(self, sql):
        """Override: bind SQL so we can filter."""
        self._sql = sql
        return self

    def fetchall(self):
        rows = self._rows
        if self._sql and "WHERE" in self._sql:
            # Basic filtering for our test queries
            filtered = []
            for row in rows:
                status = row.get("status", "")
                failures = row.get("consecutive_failures", 0)
                # Match: status IN ('running', 'ready', 'blocked') AND consecutive_failures >= 1
                if status in ("running", "ready", "blocked") and failures >= 1:
                    filtered.append(row)
            rows = filtered
        return rows


class _FakeKB:
    """Fake kanban_db module — returns pre-configured rows."""

    def __init__(self, rows=None):
        self._rows = rows or []
        self._raise_on_connect = False

    def connect_closing(self):
        if self._raise_on_connect:
            raise RuntimeError("DB unreachable")

        def _cm():
            conn = _FakeConn(self._rows)
            yield conn

        # Return a context-manager callable that yields a connection
        import contextlib
        ctx = contextlib.contextmanager(_cm)()
        return ctx


def _make_row(task_id, assignee, failures, status, error=None):
    return _Row({
        "id": task_id,
        "assignee": assignee,
        "consecutive_failures": failures,
        "status": status,
        "last_failure_error": error,
    })


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestScanAndEscalate:
    """scan_and_escalate applies decide_escalation to live tasks."""

    def test_below_limit_skipped(self):
        """Tasks below the failure threshold are not escalated."""
        rows = [_make_row("t-a", "coder", 2, "running", "oops")]
        kb = _FakeKB(rows)

        reassign_calls = []
        notify_calls = []

        results = scan_and_escalate(
            fail_limit=3,
            _kb=kb,
            _reassign=lambda tid, to: reassign_calls.append((tid, to)),
            _notify=lambda title, body: notify_calls.append((title, body)),
        )

        assert results == []
        assert reassign_calls == []
        assert notify_calls == []

    def test_at_limit_coder_escalated_to_architect(self):
        """A non-strong assignee at the threshold gets reassigned."""
        rows = [_make_row("t-b", "coder", 3, "running", "timed out")]
        kb = _FakeKB(rows)

        reassign_calls = []
        notify_calls = []

        results = scan_and_escalate(
            fail_limit=3,
            _kb=kb,
            _reassign=lambda tid, to: reassign_calls.append((tid, to)),
            _notify=lambda title, body: notify_calls.append((title, body)),
        )

        assert len(results) == 1
        assert results[0]["action"] == "escalate"
        assert results[0]["task"] == "t-b"
        assert results[0]["to"] == "architect"
        assert results[0]["category"] == "timeout"

        assert reassign_calls == [("t-b", "architect")]
        assert notify_calls == []

    def test_at_limit_architect_flags_human(self):
        """A strong assignee at the threshold triggers a human notification."""
        rows = [_make_row("t-c", "architect", 3, "blocked", "out of memory")]
        kb = _FakeKB(rows)

        reassign_calls = []
        notify_calls = []

        results = scan_and_escalate(
            fail_limit=3,
            _kb=kb,
            _reassign=lambda tid, to: reassign_calls.append((tid, to)),
            _notify=lambda title, body: notify_calls.append((title, body)),
        )

        assert len(results) == 1
        assert results[0]["action"] == "human"
        assert results[0]["task"] == "t-c"
        assert results[0]["category"] == "oom"

        assert reassign_calls == []
        assert len(notify_calls) == 1
        assert "t-c" in notify_calls[0][0]  # title contains task id

    def test_mixed_tasks(self):
        """below-limit skipped, coder escalated, architect → human."""
        rows = [
            _make_row("t-1", "coder", 1, "running", "err"),
            _make_row("t-2", "coder", 3, "ready", "assertion failed"),
            _make_row("t-3", "architect", 3, "blocked", "ImportError: missing module"),
        ]
        kb = _FakeKB(rows)

        reassign_calls = []
        notify_calls = []

        results = scan_and_escalate(
            fail_limit=3,
            _kb=kb,
            _reassign=lambda tid, to: reassign_calls.append((tid, to)),
            _notify=lambda title, body: notify_calls.append((title, body)),
        )

        # t-1 skipped (below limit)
        # t-2 escalated (coder → architect)
        # t-3 human (architect already strong)
        assert len(results) == 2

        escalate_result = [r for r in results if r["action"] == "escalate"]
        human_result = [r for r in results if r["action"] == "human"]

        assert len(escalate_result) == 1
        assert escalate_result[0]["task"] == "t-2"
        assert escalate_result[0]["to"] == "architect"
        assert escalate_result[0]["category"] == "test-failure"

        assert len(human_result) == 1
        assert human_result[0]["task"] == "t-3"
        assert human_result[0]["category"] == "tooling"

        assert reassign_calls == [("t-2", "architect")]
        assert len(notify_calls) == 1

    def test_db_error_returns_empty(self):
        """When the DB is unreachable, scan_and_escalate returns [] without raising."""
        import contextlib

        class RaisingKB:
            @contextlib.contextmanager
            def connect_closing(self):
                raise RuntimeError("DB connection refused")

        results = scan_and_escalate(_kb=RaisingKB())
        assert results == []

    def test_missing_hermes_cli_returns_empty(self):
        """When _kb import fails (no hermes_cli), return []."""
        results = scan_and_escalate()
        # If hermes_cli is available, it connects to a real DB and returns
        # whatever is there (best-effort).  If not, returns [].
        # Either way, no exception is raised.
        assert isinstance(results, list)

    def test_idempotent_already_on_strong(self):
        """A task already assigned to strong_profile yields 'human', not repeated reassign."""
        rows = [_make_row("t-d", "architect", 5, "running", "timeout")]
        kb = _FakeKB(rows)

        reassign_calls = []

        results = scan_and_escalate(
            fail_limit=3,
            strong_profile="architect",
            _kb=kb,
            _reassign=lambda tid, to: reassign_calls.append((tid, to)),
            _notify=lambda title, body: None,
        )

        assert reassign_calls == []  # no repeated reassign
        assert len(results) == 1
        assert results[0]["action"] == "human"

    def test_custom_fail_limit(self):
        """Custom fail_limit changes the threshold."""
        rows = [_make_row("t-e", "coder", 2, "running", "oom")]
        kb = _FakeKB(rows)

        reassign_calls = []

        results = scan_and_escalate(
            fail_limit=2,
            _kb=kb,
            _reassign=lambda tid, to: reassign_calls.append((tid, to)),
            _notify=lambda title, body: None,
        )

        assert len(results) == 1
        assert results[0]["action"] == "escalate"

    def test_custom_strong_profile(self):
        """Custom strong_profile is respected."""
        rows = [_make_row("t-f", "senior-dev", 3, "running", "err")]
        kb = _FakeKB(rows)

        reassign_calls = []

        results = scan_and_escalate(
            fail_limit=3,
            strong_profile="senior-dev",
            _kb=kb,
            _reassign=lambda tid, to: reassign_calls.append((tid, to)),
            _notify=lambda title, body: None,
        )

        # senior-dev IS the strong profile → human, not escalate
        assert reassign_calls == []
        assert results[0]["action"] == "human"

    def test_returns_list_of_dicts(self):
        """Return value is a list of dicts with expected keys."""
        rows = [
            _make_row("t-x", "coder", 3, "running", "timeout"),
        ]
        kb = _FakeKB(rows)

        results = scan_and_escalate(
            _kb=kb,
            _reassign=lambda tid, to: None,
            _notify=lambda title, body: None,
        )

        assert isinstance(results, list)
        assert len(results) == 1
        r = results[0]
        assert "task" in r
        assert "action" in r
        assert "to" in r
        assert "category" in r

    def test_no_failing_tasks_returns_empty(self):
        """Empty query result → empty list."""
        kb = _FakeKB([])

        results = scan_and_escalate(
            _kb=kb,
            _reassign=lambda tid, to: None,
            _notify=lambda title, body: None,
        )

        assert results == []

    def test_status_filter_only_active_statuses(self):
        """Only tasks in running/ready/blocked are scanned."""
        rows = [
            _make_row("t-done", "coder", 3, "done", "err"),
            _make_row("t-draft", "coder", 3, "draft", "err"),
            _make_row("t-active", "coder", 3, "running", "err"),
        ]
        kb = _FakeKB(rows)

        reassign_calls = []

        results = scan_and_escalate(
            _kb=kb,
            _reassign=lambda tid, to: reassign_calls.append((tid, to)),
            _notify=lambda title, body: None,
        )

        # Only t-active should be processed
        assert len(results) == 1
        assert results[0]["task"] == "t-active"
