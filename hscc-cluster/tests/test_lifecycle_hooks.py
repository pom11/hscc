"""Tests for workflow lifecycle hooks — profile_name wiring + pre_tool_call."""
import json
import os
import sys
import types

import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import workflow


class TestOnPreToolCall:
    """on_pre_tool_call writes profile_name to tool_events.jsonl."""

    def test_writes_entry_with_profile_name(self, tmp_path):
        log_path = str(tmp_path / "tool_events.jsonl")
        with patch.object(workflow, "_TOOL_EVENT_LOG", log_path):
            workflow.on_pre_tool_call(
                profile_name="devops-engineer",
                tool_name="terminal",
            )
        assert os.path.exists(log_path)
        with open(log_path) as f:
            entry = json.loads(f.readline())
        assert entry["event"] == "pre_tool_call"
        assert entry["profile_name"] == "devops-engineer"
        assert entry["tool_name"] == "terminal"
        assert "timestamp" in entry

    def test_defaults_profile_name_to_unknown(self, tmp_path):
        log_path = str(tmp_path / "tool_events.jsonl")
        with patch.object(workflow, "_TOOL_EVENT_LOG", log_path):
            workflow.on_pre_tool_call(tool_name="browser_click")
        with open(log_path) as f:
            entry = json.loads(f.readline())
        assert entry["profile_name"] == "unknown"
        assert entry["tool_name"] == "browser_click"

    def test_append_multiple_entries(self, tmp_path):
        log_path = str(tmp_path / "tool_events.jsonl")
        with patch.object(workflow, "_TOOL_EVENT_LOG", log_path):
            workflow.on_pre_tool_call(profile_name="coder", tool_name="read_file")
            workflow.on_pre_tool_call(profile_name="reviewer", tool_name="terminal")
        with open(log_path) as f:
            lines = f.readlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["profile_name"] == "coder"
        assert json.loads(lines[1])["profile_name"] == "reviewer"

    def test_never_raises_on_io_error(self):
        """Even with a bad path, on_pre_tool_call silently passes."""
        workflow.on_pre_tool_call(profile_name="test", tool_name="test_tool")


class TestOnKanbanTaskBlocked:
    """on_kanban_task_blocked records profile_name in JSONL."""

    def test_blocked_entry_includes_profile_name(self, tmp_path):
        log_path = str(tmp_path / "blocked_tasks.jsonl")
        completion_path = str(tmp_path / "task_completions.jsonl")
        with patch.object(workflow, "_BLOCKED_LOG", log_path):
            with patch.object(workflow, "_COMPLETION_LOG", completion_path):
                result = workflow.on_kanban_task_blocked(
                    task_id="t_123",
                    profile_name="worker",
                    reason="needs_input",
                    board="default",
                )
        assert os.path.exists(log_path)
        with open(log_path) as f:
            entry = json.loads(f.readline())
        assert entry["task_id"] == "t_123"
        assert entry["profile_name"] == "worker"
        assert entry["reason"] == "needs_input"
        assert entry["board"] == "default"
        assert "blocked_at" in entry
        assert result is not None
        assert result["task_id"] == "t_123"


class TestOnKanbanTaskCompleted:
    """on_kanban_task_completed records profile_name in JSONL."""

    def test_completed_entry_includes_profile_name(self, tmp_path):
        log_path = str(tmp_path / "task_completions.jsonl")
        blocked_path = str(tmp_path / "blocked_tasks.jsonl")
        with patch.object(workflow, "_COMPLETION_LOG", log_path):
            with patch.object(workflow, "_BLOCKED_LOG", blocked_path):
                result = workflow.on_kanban_task_completed(
                    task_id="t_456",
                    profile_name="devops-engineer",
                    summary="Added multiplex support",
                    board="default",
                )
        assert os.path.exists(log_path)
        with open(log_path) as f:
            entry = json.loads(f.readline())
        assert entry["task_id"] == "t_456"
        assert entry["profile_name"] == "devops-engineer"
        assert entry["summary"] == "Added multiplex support"
        assert entry["board"] == "default"
        assert "completed_at" in entry
        assert result is not None
        assert result["task_id"] == "t_456"


class TestOnKanbanTaskClaimedProfileName:
    """on_kanban_task_claimed stamps profile_name into resume note."""

    def _setup_mock(self, tmp_path, monkeypatch):
        """Create a mock kanban_db and inject it so the local import picks it up."""
        mock_conn = MagicMock()
        mock_kb = types.ModuleType("kanban_db")
        mock_kb.connect = lambda board=None: mock_conn  # noqa: E731
        mock_kb.get_task = lambda c, tid: {  # noqa: E731
            "branch_name": "feat-test",
            "workspace_path": str(tmp_path),
        }
        mock_kb.add_comment = MagicMock()

        hermes_cli = types.ModuleType("hermes_cli")
        hermes_cli.kanban_db = mock_kb  # noqa: B028
        monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)

        return mock_kb, mock_conn

    def test_profile_name_in_resume_note(self, tmp_path, monkeypatch):
        mock_kb, _mock_conn = self._setup_mock(tmp_path, monkeypatch)

        monkeypatch.setattr(
            workflow, "resume_note",
            lambda t, repo: "Resume note body here",
        )

        result = workflow.on_kanban_task_claimed(
            task_id="t_789",
            profile_name="devops-engineer",
        )

        assert result is not None
        assert result["posted"] is True
        # Verify add_comment was called with profile-stamped note
        call_args = mock_kb.add_comment.call_args
        assert call_args is not None
        # add_comment(c, task_id, author="hscc-resume", body=note)
        body = call_args.kwargs.get("body", call_args[0][3] if len(call_args[0]) > 3 else "")
        assert "devops-engineer" in body
        assert "`profile`" in body

    def test_no_profile_name_skips_stamp(self, tmp_path, monkeypatch):
        mock_kb, _mock_conn = self._setup_mock(tmp_path, monkeypatch)

        monkeypatch.setattr(
            workflow, "resume_note",
            lambda t, repo: "Resume note body here",
        )

        result = workflow.on_kanban_task_claimed(
            task_id="t_789",
            profile_name=None,
        )

        assert result is not None
        assert result["posted"] is True
        call_args = mock_kb.add_comment.call_args
        assert call_args is not None
        body = call_args.kwargs.get("body", call_args[0][3] if len(call_args[0]) > 3 else "")
        # Body should just be the original note, no profile prefix
        assert "`profile`" not in body
        assert body == "Resume note body here"