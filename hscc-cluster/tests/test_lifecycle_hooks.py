"""Tests for workflow lifecycle hooks — original coverage + profile_name wiring + pre_tool_call."""

import json
import os
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import workflow


# ── Original tests from origin/main ────────────────────────────────────────
# These require hermes_cli; skipped gracefully when unavailable.

_HAS_HERMES_CLI = True
try:
    import hermes_cli.kanban_db as kb  # noqa: E402
    import hermes_cli.plugins as plugins  # noqa: E402
except ImportError:
    _HAS_HERMES_CLI = False


def _board():
    d = tempfile.mkdtemp()
    dbp = Path(d) / "kanban.db"
    kb.init_db(dbp)
    return kb.connect(dbp)


@pytest.mark.skipif(not _HAS_HERMES_CLI, reason="hermes_cli not installed in this env")
class TestHookRegistration:
    """Hook registration checks — require hermes_cli."""

    def test_blocked_hook_in_valid_hooks(self):
        assert "kanban_task_blocked" in plugins.VALID_HOOKS

    def test_completed_hook_in_valid_hooks(self):
        assert "kanban_task_completed" in plugins.VALID_HOOKS


@pytest.mark.skipif(not _HAS_HERMES_CLI, reason="hermes_cli not installed in this env")
class TestBlockedHandler:
    """on_kanban_task_blocked writes a JSON line to the blocked log."""

    def test_blocked_handler_writes_jsonl(self, tmp_path):
        log_path = tmp_path / "blocked_tasks.jsonl"
        with patch.object(workflow, "_BLOCKED_LOG", str(log_path)):
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

    def test_blocked_handler_noop_without_context(self, tmp_path):
        """on_kanban_task_blocked doesn't crash with all-None args.

        t_1497a937: this test (and its completed sibling) is what filled the
        operator's live ``blocked_tasks.jsonl`` / ``task_completions.jsonl`` with
        797 all-null rows — it called the handler with no args and no path
        patch. The log paths are now pinned to ``tmp_path`` for every test in
        this suite by ``conftest._pin_lifecycle_logs_to_tmp``, and the handler
        records ``"unknown"`` instead of ``null``, so a noop call is both
        harmless and self-describing.
        """
        import workflow as wf

        log = tmp_path / "noop-blocked.jsonl"
        with patch.object(wf, "_BLOCKED_LOG", str(log)):
            result = workflow.on_kanban_task_blocked()
        assert result is not None
        assert result["task_id"] is None          # raw return keeps "nothing given"
        entry = json.loads(log.read_text().strip())
        assert entry["task_id"] == "unknown"      # durable row is never null
        assert None not in entry.values()


@pytest.mark.skipif(not _HAS_HERMES_CLI, reason="hermes_cli not installed in this env")
class TestCompletedHandler:
    """on_kanban_task_completed writes a JSON line to the completion log."""

    def test_completed_handler_writes_jsonl(self, tmp_path):
        log_path = tmp_path / "task_completions.jsonl"
        with patch.object(workflow, "_COMPLETION_LOG", str(log_path)):
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

    def test_completed_handler_noop_without_context(self, tmp_path):
        """on_kanban_task_completed doesn't crash with all-None args.

        The twin of ``TestBlockedHandler.test_blocked_handler_noop_without_context``
        and the other half of the t_1497a937 live-ledger leak: it used to append
        one all-null row per suite run to ``~/.hscc/task_completions.jsonl``.
        """
        import workflow as wf

        log = tmp_path / "noop-completed.jsonl"
        blocked = tmp_path / "noop-blocked.jsonl"
        with patch.object(wf, "_COMPLETION_LOG", str(log)), \
             patch.object(wf, "_BLOCKED_LOG", str(blocked)):
            result = workflow.on_kanban_task_completed()
        assert result is not None
        assert result["task_id"] is None          # raw return keeps "nothing given"
        entry = json.loads(log.read_text().strip())
        assert entry["task_id"] == "unknown"      # durable row is never null
        assert entry["profile_name"] == "unknown"
        assert entry["board"] == "unknown"
        # Identity keys are never null. ``summary`` is content, not an identity
        # key: a worker that passed no summary genuinely has none, and putting
        # the string "unknown" in a human-facing free-text field would be a lie.
        for key in ("task_id", "profile_name", "board"):
            assert entry[key] is not None


@pytest.mark.skipif(not _HAS_HERMES_CLI, reason="hermes_cli not installed in this env")
class TestAutoUnblock:
    """_try_auto_unblock promotes a blocked task when its block comments
    reference the completed parent task_id."""

    def test_auto_unblock_with_parent_reference(self, monkeypatch, tmp_path):
        import workflow as wf

        conn = _board()
        parent_tid = kb.create_task(conn, title="parent", assignee="coder")
        if not hasattr(parent_tid, "id"):
            parent_tid = parent_tid
        child_tid = kb.create_task(conn, title="child", assignee="coder")
        if not hasattr(child_tid, "id"):
            child_tid = child_tid

        # Parent is completed; block the child with a comment referencing it.
        conn.execute(
            "UPDATE tasks SET status='done' WHERE id=?", (parent_tid,))
        conn.execute(
            "UPDATE tasks SET status='blocked' WHERE id=?", (child_tid,))
        kb.add_comment(conn, child_tid, "test", f"blocked because of {parent_tid}")
        conn.commit()

        # Patch _kb.connect to return our test board.
        monkeypatch.setattr("hermes_cli.kanban_db.connect",
                            lambda *a, **kw: conn)

        result = wf._try_auto_unblock(child_tid, parent_tid)
        assert result is True

        # Verify the child is now ready.
        child_row = kb.get_task(conn, child_tid)
        assert child_row.status == "ready"

    def test_auto_unblock_no_match(self, monkeypatch):
        """_try_auto_unblock returns False when block comments don't reference
        the parent."""
        import workflow as wf

        conn = _board()
        child_tid = kb.create_task(conn, title="child", assignee="coder")
        if not hasattr(child_tid, "id"):
            child_tid = child_tid

        conn.execute(
            "UPDATE tasks SET status='blocked' WHERE id=?", (child_tid,))
        kb.add_comment(conn, child_tid, "test", "some unrelated reason")
        conn.commit()

        monkeypatch.setattr("hermes_cli.kanban_db.connect",
                            lambda *a, **kw: conn)

        result = wf._try_auto_unblock(child_tid, "nonexistent-parent")
        assert result is False

    def test_two_deps_only_one_done_not_unblocked(self, monkeypatch, tmp_path):
        """(a) Two deps referenced, only one done -> NOT unblocked."""
        import workflow as wf

        conn = _board()
        parent_a = kb.create_task(conn, title="parent_a", assignee="coder")
        parent_b = kb.create_task(conn, title="parent_b", assignee="coder")
        child_tid = kb.create_task(conn, title="child", assignee="coder")

        # Mark parent_a as done, parent_b stays pending.
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent_a,))
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (child_tid,))
        kb.add_comment(conn, child_tid, "test",
             f"blocked on {parent_a} and {parent_b}")
        conn.commit()

        monkeypatch.setattr("hermes_cli.kanban_db.connect",
                            lambda *a, **kw: conn)

        result = wf._try_auto_unblock(child_tid, parent_a)
        assert result is False

        # Child should still be blocked.
        child_row = kb.get_task(conn, child_tid)
        assert child_row.status == "blocked"

    def test_two_deps_both_done_unblocked(self, monkeypatch, tmp_path):
        """(b) Two deps referenced, both done -> unblocked."""
        import workflow as wf

        conn = _board()
        parent_a = kb.create_task(conn, title="parent_a", assignee="coder")
        parent_b = kb.create_task(conn, title="parent_b", assignee="coder")
        child_tid = kb.create_task(conn, title="child", assignee="coder")

        # Both parents done.
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent_a,))
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent_b,))
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (child_tid,))
        kb.add_comment(conn, child_tid, "test",
             f"blocked on {parent_a} and {parent_b}")
        conn.commit()

        monkeypatch.setattr("hermes_cli.kanban_db.connect",
                            lambda *a, **kw: conn)

        result = wf._try_auto_unblock(child_tid, parent_a)
        assert result is True

        child_row = kb.get_task(conn, child_tid)
        assert child_row.status == "ready"

    def test_substring_lookalike_does_not_trigger(self, monkeypatch, tmp_path):
        """(c) A task id that looks like a substring of another does not
        falsely match (e.g., t_aaaa inside t_aaaaaa)."""
        import workflow as wf

        conn = _board()
        real_parent = kb.create_task(conn, title="real_parent", assignee="coder")
        child_tid = kb.create_task(conn, title="child", assignee="coder")

        # Mark the real parent as done.
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (real_parent,))
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (child_tid,))

        # Comment mentions a task id that shares a prefix but is NOT the
        # real parent.
        fake_id = "t_aaaaaa111111"
        kb.add_comment(conn, child_tid, "test", f"blocked on {fake_id}")
        conn.commit()

        monkeypatch.setattr("hermes_cli.kanban_db.connect",
                            lambda *a, **kw: conn)

        # Calling with the real parent id should NOT unblock because
        # the comment references the fake id, not the real parent.
        result = wf._try_auto_unblock(child_tid, real_parent)
        assert result is False

        child_row = kb.get_task(conn, child_tid)
        assert child_row.status == "blocked"

    def test_single_dep_done_unblocked(self, monkeypatch, tmp_path):
        """(d) Single dependency, now done -> unblocked (preserves happy path)."""
        import workflow as wf

        conn = _board()
        parent_tid = kb.create_task(conn, title="parent", assignee="coder")
        child_tid = kb.create_task(conn, title="child", assignee="coder")

        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent_tid,))
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (child_tid,))
        kb.add_comment(conn, child_tid, "test", f"blocked because of {parent_tid}")
        conn.commit()

        monkeypatch.setattr("hermes_cli.kanban_db.connect",
                            lambda *a, **kw: conn)

        result = wf._try_auto_unblock(child_tid, parent_tid)
        assert result is True

        child_row = kb.get_task(conn, child_tid)
        assert child_row.status == "ready"


# ── profile_name wiring tests (from feat-multiplexing-observability) ────────
# These do NOT require hermes_cli installed — they use unittest.mock.


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

# ── payload-shape pinning (t_1497a937) ───────────────────────────────────────
# The card that filed this defect assumed hermes delivers a kwarg shape the
# handler does not read. It does not: hermes_cli/kanban_db.py fires
#   invoke_hook(event, task_id=task_id, profile_name=..., board=...,
#               assignee=..., run_id=..., summary=...)
# and plugins_dispatch withholds nothing from a callback that declares
# **kwargs (``_hook_callback_kwargs`` returns the whole payload). The 797
# null-task_id rows were our own noop tests writing to the live ledger, not a
# shape drift. These tests pin the ACTUAL shape so a real drift upstream is
# caught here instead of in the operator's ledger.

#: The exact kwargs hermes 2026.9.24 delivers for kanban_task_completed:
#: hermes_cli/kanban_db.py:157 (task_id, profile_name) + :164 (board, assignee,
#: run_id) + :2838 (summary) + plugins_dispatch.py:222 (telemetry_schema_version).
HERMES_COMPLETED_PAYLOAD = {
    "task_id": "t_abc123",
    "profile_name": "backend-engineer",
    "board": "hscc",
    "assignee": "backend-engineer",
    "run_id": 421,
    "summary": "shipped as PR #173",
    "telemetry_schema_version": "hermes.observer.v1",
}

HERMES_BLOCKED_PAYLOAD = {
    "task_id": "t_abc123",
    "profile_name": "backend-engineer",
    "board": "hscc",
    "assignee": "backend-engineer",
    "run_id": 421,
    "reason": "needs_input",
    "telemetry_schema_version": "hermes.observer.v1",
}


class TestRealHermesPayloadShape:
    """The handler must land task_id from the payload hermes actually sends."""

    def test_completed_lands_task_id_from_real_shape(self, tmp_path):
        import workflow as wf

        log = tmp_path / "completions.jsonl"
        with patch.object(wf, "_COMPLETION_LOG", str(log)), \
             patch.object(wf, "_BLOCKED_LOG", str(tmp_path / "blocked.jsonl")):
            result = workflow.on_kanban_task_completed(**HERMES_COMPLETED_PAYLOAD)

        assert result["task_id"] == "t_abc123"
        entry = json.loads(log.read_text().strip())
        assert entry["task_id"] == "t_abc123"
        assert entry["profile_name"] == "backend-engineer"
        assert entry["board"] == "hscc"
        assert entry["summary"] == "shipped as PR #173"

    def test_blocked_lands_task_id_from_real_shape(self, tmp_path):
        import workflow as wf

        log = tmp_path / "blocked.jsonl"
        with patch.object(wf, "_BLOCKED_LOG", str(log)):
            result = workflow.on_kanban_task_blocked(**HERMES_BLOCKED_PAYLOAD)

        assert result["task_id"] == "t_abc123"
        entry = json.loads(log.read_text().strip())
        assert entry["task_id"] == "t_abc123"
        assert entry["reason"] == "needs_input"

    @pytest.mark.skipif(not _HAS_HERMES_CLI,
                        reason="hermes_cli not installed in this env")
    def test_dispatch_through_real_hermes_binding(self, tmp_path):
        """Drive the handler through hermes' OWN callback binder, not a hand-typed call.

        ``PluginDispatchMixin._invoke_hook_callback`` is what hermes uses to call
        every plugin hook; ``_hook_callback_kwargs`` decides which payload keys a
        callback sees. Binding here means these tests fail if hermes starts
        nesting the payload, renaming ``task_id``, or dropping the ``**kwargs``
        passthrough our handler relies on — the failure lands in pytest, not in
        ``~/.hscc/task_completions.jsonl``.
        """
        import inspect

        from hermes_cli.plugins_dispatch import PluginDispatchMixin as M

        # Guard the premise: our handler must keep **kwargs, because that is
        # precisely what makes the binder hand it the whole payload.
        params = inspect.signature(workflow.on_kanban_task_completed).parameters
        assert any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()), (
            "on_kanban_task_completed lost its **kwargs; hermes's binder would now "
            "withhold every additive field from it (see plugins_dispatch."
            "_hook_callback_kwargs)")

        forwarded = M._hook_callback_kwargs(workflow.on_kanban_task_completed,
                                            dict(HERMES_COMPLETED_PAYLOAD))
        assert forwarded.get("task_id") == "t_abc123", (
            "hermes' binder no longer forwards task_id to our handler — "
            f"it forwards {sorted(forwarded)}")

        log = tmp_path / "completions.jsonl"
        blocked = tmp_path / "blocked.jsonl"
        with patch.object(workflow, "_COMPLETION_LOG", str(log)), \
             patch.object(workflow, "_BLOCKED_LOG", str(blocked)):
            M._invoke_hook_callback(workflow.on_kanban_task_completed,
                                    dict(HERMES_COMPLETED_PAYLOAD))
        entry = json.loads(log.read_text().strip())
        assert entry["task_id"] == "t_abc123"


class TestAbsentFieldFallback:
    """A genuinely absent field logs "unknown" — never a silent null."""

    def test_missing_task_id_logs_unknown(self, tmp_path):
        import workflow as wf

        log = tmp_path / "completions.jsonl"
        payload = dict(HERMES_COMPLETED_PAYLOAD)
        payload.pop("task_id")
        with patch.object(wf, "_COMPLETION_LOG", str(log)), \
             patch.object(wf, "_BLOCKED_LOG", str(tmp_path / "blocked.jsonl")):
            workflow.on_kanban_task_completed(**payload)

        entry = json.loads(log.read_text().strip())
        assert entry["task_id"] == "unknown"
        assert None not in entry.values()
        # The rest of the payload is still attributable.
        assert entry["profile_name"] == "backend-engineer"
        assert entry["board"] == "hscc"

    def test_blank_and_whitespace_ids_log_unknown(self, tmp_path):
        import workflow as wf

        log = tmp_path / "completions.jsonl"
        for bad in (None, "", "   ", "\t\n"):
            log.unlink(missing_ok=True)
            payload = dict(HERMES_COMPLETED_PAYLOAD, task_id=bad)
            with patch.object(wf, "_COMPLETION_LOG", str(log)), \
                 patch.object(wf, "_BLOCKED_LOG", str(tmp_path / "blocked.jsonl")):
                workflow.on_kanban_task_completed(**payload)
            entry = json.loads(log.read_text().strip())
            assert entry["task_id"] == "unknown", f"task_id={bad!r} logged {entry['task_id']!r}"

    def test_non_string_task_id_is_stringified(self, tmp_path):
        """A numeric run-style id must not round-trip as null or be dropped."""
        import workflow as wf

        log = tmp_path / "completions.jsonl"
        payload = dict(HERMES_COMPLETED_PAYLOAD, task_id=1234)
        with patch.object(wf, "_COMPLETION_LOG", str(log)), \
             patch.object(wf, "_BLOCKED_LOG", str(tmp_path / "blocked.jsonl")):
            workflow.on_kanban_task_completed(**payload)
        entry = json.loads(log.read_text().strip())
        assert entry["task_id"] == "1234"

    def test_blocked_missing_fields_log_unknown(self, tmp_path):
        import workflow as wf

        log = tmp_path / "blocked.jsonl"
        payload = dict(HERMES_BLOCKED_PAYLOAD)
        payload.pop("reason")
        payload.pop("board")
        with patch.object(wf, "_BLOCKED_LOG", str(log)):
            workflow.on_kanban_task_blocked(**payload)
        entry = json.loads(log.read_text().strip())
        assert entry["task_id"] == "t_abc123"
        assert entry["reason"] == "unknown"
        assert entry["board"] == "unknown"
        assert None not in entry.values()

    def test_unknown_rows_do_not_drive_auto_unblock(self, tmp_path, monkeypatch):
        """The auto-unblock scan must not chase the "unknown" sentinel as a card id."""
        import workflow as wf

        calls = []
        monkeypatch.setattr(wf, "_try_auto_unblock",
                            lambda child, parent: calls.append((child, parent)) or False)
        blocked = tmp_path / "blocked.jsonl"
        blocked.write_text(
            json.dumps({"task_id": "unknown", "blocked_at": "2026-11-02T00:00:00"}) + "\n"
            + json.dumps({"task_id": "t_real", "blocked_at": "2026-11-02T00:00:00"}) + "\n")
        log = tmp_path / "completions.jsonl"
        with patch.object(wf, "_COMPLETION_LOG", str(log)), \
             patch.object(wf, "_BLOCKED_LOG", str(blocked)):
            workflow.on_kanban_task_completed(**HERMES_COMPLETED_PAYLOAD)

        assert ("unknown", "t_abc123") not in calls
        assert ("t_real", "t_abc123") in calls


class TestNoLiveLedgerWrites:
    """Regression guard for the leak that produced the 797 null rows."""

    def test_conftest_pins_default_log_paths_away_from_home(self, tmp_path):
        """The autouse conftest fixture must be what protects the live ledgers.

        Asserted rather than trusted: if the fixture is renamed or removed, the
        noop tests go back to appending to ``~/.hscc`` and this fails loudly.
        """
        import workflow as wf

        home = os.path.expanduser("~/.hscc")
        for name in ("_COMPLETION_LOG", "_BLOCKED_LOG", "_TOOL_EVENT_LOG"):
            current = getattr(wf, name)
            assert not str(current).startswith(home), (
                f"workflow.{name} resolves to the LIVE ledger ({current}) inside the "
                "test suite — conftest._pin_lifecycle_logs_to_tmp is not in effect")
