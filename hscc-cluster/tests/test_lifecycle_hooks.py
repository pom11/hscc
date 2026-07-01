"""Tests for the new kanban lifecycle hooks (blocked + completed).

Skipped where hermes_cli isn't installed or hscc_daemon isn't importable,
matching the existing test_dispatch_hook.py pattern.
"""

import json
import tempfile
from pathlib import Path

import pytest

kb = pytest.importorskip("hermes_cli.kanban_db",
                         reason="hermes_cli not installed in this env")
import hermes_cli.plugins as plugins  # noqa: E402

from . import workflow  # noqa: E402


def _board():
    d = tempfile.mkdtemp()
    dbp = Path(d) / "kanban.db"
    kb.init_db(dbp)
    return kb.connect(dbp)


# ── Hook registration ───────────────────────────────────────────────────────

def test_blocked_hook_in_valid_hooks():
    assert "kanban_task_blocked" in plugins.VALID_HOOKS


def test_completed_hook_in_valid_hooks():
    assert "kanban_task_completed" in plugins.VALID_HOOKS


# ── blocked handler ─────────────────────────────────────────────────────────

def test_blocked_handler_writes_jsonl(tmp_path):
    """on_kanban_task_blocked writes a JSON line to the blocked log."""
    log_path = tmp_path / "blocked_tasks.jsonl"
    monkeypatch_blocked_log(log_path)

    result = workflow.on_kanban_task_blocked(
        task_id="test-123",
        profile_name="coder",
        reason="needs_input",
        board="default"
    )

    assert result is not None
    assert result["task_id"] == "test-123"
    entries = log_path.read_text().strip().split("\n")
    entry = json.loads(entries[0])
    assert entry["task_id"] == "test-123"
    assert entry["profile_name"] == "coder"
    assert entry["reason"] == "needs_input"
    assert entry["board"] == "default"
    assert "blocked_at" in entry


def test_blocked_handler_noop_without_context():
    """on_kanban_task_blocked doesn't crash with all-None args."""
    result = workflow.on_kanban_task_blocked()
    assert result is not None
    assert result["task_id"] is None


# ── completed handler ───────────────────────────────────────────────────────

def test_completed_handler_writes_jsonl(tmp_path):
    """on_kanban_task_completed writes a JSON line to the completion log."""
    log_path = tmp_path / "task_completions.jsonl"
    monkeypatch_completion_log(log_path)

    result = workflow.on_kanban_task_completed(
        task_id="test-456",
        profile_name="reviewer",
        summary="All checklist items done",
        board="default"
    )

    assert result is not None
    assert result["task_id"] == "test-456"
    entries = log_path.read_text().strip().split("\n")
    entry = json.loads(entries[0])
    assert entry["task_id"] == "test-456"
    assert entry["profile_name"] == "reviewer"
    assert entry["summary"] == "All checklist items done"
    assert "completed_at" in entry


def test_completed_handler_noop_without_context():
    """on_kanban_task_completed doesn't crash with all-None args."""
    result = workflow.on_kanban_task_completed()
    assert result is not None
    assert result["task_id"] is None


# ── auto-unblock ────────────────────────────────────────────────────────────

def test_auto_unblock_with_parent_reference(monkeypatch, tmp_path):
    """_try_auto_unblock promotes a blocked task when its block comments
    reference the completed parent task_id."""
    import workflow as wf

    conn = _board()
    parent_tid = kb.create_task(conn, title="parent", assignee="coder")
    if not hasattr(parent_tid, "id"):
        parent_tid = parent_tid
    child_tid = kb.create_task(conn, title="child", assignee="coder")
    if not hasattr(child_tid, "id"):
        child_tid = child_tid

    # Block the child with a comment referencing the parent.
    conn.execute(
        "UPDATE tasks SET status='blocked' WHERE id=?", (child_tid,))
    conn.execute(
        "INSERT INTO task_comments (task_id, author, body) VALUES (?, ?, ?)",
        (child_tid, "test", f"blocked because of {parent_tid}"))
    conn.commit()

    # Patch _kb.connect to return our test board.
    monkeypatch.setattr("hermes_cli.kanban_db.connect",
                        lambda *a, **kw: conn)

    result = wf._try_auto_unblock(child_tid, parent_tid)
    assert result is True

    # Verify the child is now ready.
    child_row = kb.get_task(conn, child_tid)
    assert child_row["status"] == "ready"


def test_auto_unblock_no_match(monkeypatch):
    """_try_auto_unblock returns False when block comments don't reference
    the parent."""
    import workflow as wf

    conn = _board()
    child_tid = kb.create_task(conn, title="child", assignee="coder")
    if not hasattr(child_tid, "id"):
        child_tid = child_tid

    conn.execute(
        "UPDATE tasks SET status='blocked' WHERE id=?", (child_tid,))
    conn.execute(
        "INSERT INTO task_comments (task_id, author, body) VALUES (?, ?, ?)",
        (child_tid, "test", "some unrelated reason"))
    conn.commit()

    monkeypatch.setattr("hermes_cli.kanban_db.connect",
                        lambda *a, **kw: conn)

    result = wf._try_auto_unblock(child_tid, "nonexistent-parent")
    assert result is False


# ── helpers ─────────────────────────────────────────────────────────────────

def monkeypatch_blocked_log(log_path):
    """Temporarily redirect the blocked log to a test path."""
    import os
    from unittest.mock import patch

    with patch.object(workflow, "_BLOCKED_LOG", str(log_path)):
        yield


def monkeypatch_completion_log(log_path):
    """Temporarily redirect the completion log to a test path."""
    from unittest.mock import patch

    with patch.object(workflow, "_COMPLETION_LOG", str(log_path)):
        yield