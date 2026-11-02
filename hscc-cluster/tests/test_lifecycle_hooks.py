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

    def test_blocked_handler_noop_without_context(self):
        """on_kanban_task_blocked doesn't crash with all-None args."""
        result = workflow.on_kanban_task_blocked()
        assert result is not None
        assert result["task_id"] is None


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

    def test_completed_handler_noop_without_context(self):
        """on_kanban_task_completed doesn't crash with all-None args."""
        result = workflow.on_kanban_task_completed()
        assert result is not None
        assert result["task_id"] is None


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

    def test_defaults_profile_name_to_unknown(self, tmp_path, monkeypatch):
        """Sentinel ONLY when neither the kwarg nor hermes_cli resolves (t_444b1bb6).

        On main this test pinned the old read-only-the-kwarg behaviour. The card
        replaces it: an absent kwarg now falls back to
        ``get_active_profile_name()``, so the sentinel is reachable only when that
        lookup is genuinely unresolvable — pin the fence unresolvable HERE instead
        of hoping the test interpreter has no hermes_cli (it usually does).
        """
        monkeypatch.setattr(workflow, "_active_profile_name", lambda: None)
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

    def test_never_raises_on_io_error(self, tmp_path):
        """Even with a bad path, on_pre_tool_call silently passes.

        LEAK FIXED HERE (t_444b1bb6): on main this test patched nothing, so every
        ``scripts/run_tests.sh`` run appended a ``profile_name="test"`` row to the
        operator's LIVE ``~/.hscc/tool_events.jsonl`` — measured at 1538 of 5829 rows.
        Same defect class t_1497a937 fixed for task_completions/blocked_tasks, whose
        autouse conftest pin is still open in PR #179 and therefore not in this base.
        The path is now an unwritable one *inside tmp_path*, so the failure it is
        testing (open() raising) is exercised without touching production.
        """
        bad = str(tmp_path / "no-such-dir" / "tool_events.jsonl")
        with patch.object(workflow, "_TOOL_EVENT_LOG", bad):
            assert workflow.on_pre_tool_call(profile_name="test",
                                             tool_name="test_tool") is None
        assert not os.path.exists(bad)


# ── profile_name FALLBACK (t_444b1bb6) ──────────────────────────────────────
#
# LIVE DEFECT: hermes' pre_tool_call fire site
# (``hermes_cli/plugins.py::_get_pre_tool_call_directive_details``) forwards
# tool_name/args/task_id/session_id/tool_call_id/turn_id/api_request_id/
# middleware_trace and NEVER ``profile_name``. The handler read only the kwarg,
# so every live ``~/.hscc/tool_events.jsonl`` row carried the ``"unknown"``
# sentinel — the card measured 4291 such rows — and ``hscc stats``'
# ``tool_calls_by_profile`` collapsed into a single bucket.
#
# Contract pinned below, matching hermes' own ``kanban_db._hook_profile_name()``:
# explicit kwarg wins, else ``get_active_profile_name()``, else the sentinel —
# and the hook NEVER raises.
#
# These patch the log path themselves: the autouse ``_pin_lifecycle_logs_to_tmp``
# conftest fixture that would make the tmp default structural is on PR #179 and is
# not in main yet, so on this base every test that can write MUST name its path.


class TestPreToolCallProfileFallback:
    """profile_name: kwarg > get_active_profile_name() > "unknown"."""

    def _row(self, tmp_path, monkeypatch, **kwargs):
        log_path = str(tmp_path / "tool_events.jsonl")
        with patch.object(workflow, "_TOOL_EVENT_LOG", log_path):
            workflow.on_pre_tool_call(**kwargs)
        with open(log_path) as f:
            return json.loads(f.readline())

    def test_kwarg_present_wins_over_resolvable_hermes(self, tmp_path, monkeypatch):
        """(a) A sent profile_name is authoritative even when the fallback works.

        This is the guard against a future fire site that DOES send the kwarg:
        the caller's answer must beat our own guess, or a multiplexed host
        profile would re-label a served profile's rows.
        """
        monkeypatch.setattr(workflow, "_active_profile_name",
                            lambda: "backend-engineer")
        row = self._row(tmp_path, monkeypatch,
                        profile_name="reviewer", tool_name="terminal")
        assert row["profile_name"] == "reviewer"

    def test_kwarg_absent_falls_back_to_active_profile(self, tmp_path, monkeypatch):
        """(b) No kwarg + hermes_cli resolvable -> the ACTIVE profile name.

        This is the exact live shape (the real payload has no profile_name key),
        so this test is the one that fails on main.
        """
        monkeypatch.setattr(workflow, "_active_profile_name",
                            lambda: "ops-engineer")
        row = self._row(tmp_path, monkeypatch, tool_name="read_file")
        assert row["profile_name"] == "ops-engineer"

    def test_both_unavailable_logs_unknown_and_never_raises(
            self, tmp_path, monkeypatch):
        """(c) No kwarg + no hermes_cli -> "unknown", and the hook stays fail-open."""
        monkeypatch.setattr(workflow, "_active_profile_name", lambda: None)
        row = self._row(tmp_path, monkeypatch, tool_name="browser_click")
        assert row["profile_name"] == "unknown"

    def test_blank_kwarg_falls_through_not_stamped(self, tmp_path, monkeypatch):
        """A blank/None/whitespace kwarg counts as NOT sent.

        An empty string in the ledger is exactly as unattributable as the
        sentinel, and the fallback is strictly more informative.
        """
        monkeypatch.setattr(workflow, "_active_profile_name",
                            lambda: "backend-engineer")
        for blank in (None, "", "   ", "\t\n"):
            (tmp_path / "tool_events.jsonl").unlink(missing_ok=True)
            row = self._row(tmp_path, monkeypatch,
                            profile_name=blank, tool_name="terminal")
            assert row["profile_name"] == "backend-engineer", (
                f"profile_name={blank!r} stamped {row['profile_name']!r}")

    def test_non_string_kwarg_is_stringified(self, tmp_path, monkeypatch):
        monkeypatch.setattr(workflow, "_active_profile_name", lambda: "worker")
        row = self._row(tmp_path, monkeypatch, profile_name=42, tool_name="x")
        assert row["profile_name"] == "42"


class TestActiveProfileNameFence:
    """The hermes_cli lookup itself: fail-open, never raises to the caller.

    ``on_pre_tool_call`` runs on the hot path of EVERY tool call in every
    profile, so a raising lookup would be an availability bug, not just an
    observability one.
    """

    @pytest.mark.skipif(not _HAS_HERMES_CLI,
                        reason="hermes_cli not installed in this env")
    def test_real_lookup_names_the_active_profile(self, monkeypatch):
        """Against real hermes_cli: the name must be non-empty and stable.

        Hermes points HERMES_HOME at the profile dir, so the active profile is
        derivable in any profile session; the sentinel here would mean the fence
        silently lost hermes_cli (the failure mode this card is fixing).
        """
        from hermes_cli.profiles import get_active_profile_name

        expected = get_active_profile_name()
        assert workflow._active_profile_name() == expected
        assert expected and expected.strip() == expected

    def test_unimportable_hermes_cli_returns_none(self, monkeypatch):
        """hermes_cli present in sys.modules as None -> ImportError -> None.

        ``None`` in ``sys.modules`` is CPython's mark for a failed import; it
        reproduces a partial install / foreign venv without touching sys.path.
        """
        monkeypatch.setitem(sys.modules, "hermes_cli.profiles", None)
        assert workflow._active_profile_name() is None

    def test_lookup_that_raises_returns_none(self, monkeypatch):
        """A resolvable module whose call explodes must not reach the hook."""
        broken = types.ModuleType("hermes_cli.profiles")

        def _boom():
            raise RuntimeError("profile config unreadable")

        broken.get_active_profile_name = _boom
        monkeypatch.setitem(sys.modules, "hermes_cli.profiles", broken)
        assert workflow._active_profile_name() is None

    def test_empty_active_name_is_not_stamped(self, monkeypatch):
        """A blank answer from hermes is not a usable identity -> None."""
        blank = types.ModuleType("hermes_cli.profiles")
        blank.get_active_profile_name = lambda: "   "
        monkeypatch.setitem(sys.modules, "hermes_cli.profiles", blank)
        assert workflow._active_profile_name() is None

    def test_hook_never_raises_even_when_lookup_and_io_both_fail(self, tmp_path,
                                                                 monkeypatch):
        def _boom():
            raise RuntimeError("lookup exploded")

        monkeypatch.setattr(workflow, "_active_profile_name", _boom)
        with patch.object(workflow, "_TOOL_EVENT_LOG",
                          str(tmp_path / "nope" / "deep" / "tool_events.jsonl")):
            assert workflow.on_pre_tool_call(tool_name="terminal") is None


@pytest.mark.skipif(not _HAS_HERMES_CLI,
                    reason="hermes_cli not installed in this env")
class TestPreToolCallThroughRealHermesBinder:
    """Bind through hermes' OWN callback binder with the real fire-site payload.

    ``PluginDispatchMixin._invoke_hook_callback`` is what hermes uses to call
    every plugin hook, and ``_hook_callback_kwargs`` decides which payload keys a
    callback sees. Driving the handler through it — with the key list copied from
    ``plugins.py``'s ``invoke_lifecycle_hook("pre_tool_call", ...)`` call — means
    a payload drift fails in pytest instead of quietly re-deadening the
    per-profile tool view. Same precedent as #179's completion-binder test.
    """

    # The kwargs hermes' pre_tool_call fire site actually passes
    # (hermes_cli/plugins.py:1871), plus the observer envelope invoke_hook adds.
    HERMES_PRE_TOOL_CALL_PAYLOAD = {
        "tool_name": "terminal",
        "args": {"command": "echo hi"},
        "task_id": "t_abc123",
        "session_id": "sess_1",
        "tool_call_id": "call_1",
        "turn_id": "turn_1",
        "api_request_id": "api_1",
        "middleware_trace": [],
        "telemetry_schema_version": 1,
    }

    def test_binder_forwards_no_profile_name(self):
        """Guard the PREMISE: hermes still does not send profile_name.

        If hermes ever starts sending it, this fails and the fallback above gets
        re-verified (and the live ledger's sentinel count stops growing).
        """
        import inspect

        from hermes_cli.plugins_dispatch import PluginDispatchMixin as M

        params = inspect.signature(workflow.on_pre_tool_call).parameters
        assert any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()), (
            "on_pre_tool_call lost its **kwargs; hermes' binder would now withhold "
            "every additive field from it (plugins_dispatch._hook_callback_kwargs)")

        forwarded = M._hook_callback_kwargs(
            workflow.on_pre_tool_call, dict(self.HERMES_PRE_TOOL_CALL_PAYLOAD))
        assert forwarded.get("tool_name") == "terminal"
        assert "profile_name" not in forwarded, (
            "hermes now sends profile_name — revisit _ledger_profile_name and the "
            "card premise; the kwarg branch must stay authoritative")

    def test_real_payload_row_carries_a_real_profile(self, tmp_path, monkeypatch):
        """The live end-to-end shape: no profile_name in, a real name out."""
        from hermes_cli.profiles import get_active_profile_name
        from hermes_cli.plugins_dispatch import PluginDispatchMixin as M

        log = tmp_path / "tool_events.jsonl"
        monkeypatch.setattr(workflow, "_TOOL_EVENT_LOG", str(log))
        M._invoke_hook_callback(workflow.on_pre_tool_call,
                                dict(self.HERMES_PRE_TOOL_CALL_PAYLOAD))

        row = json.loads(log.read_text().strip())
        assert row["profile_name"] == get_active_profile_name()
        assert row["profile_name"] != "unknown", (
            "fallback resolved to the sentinel — the per-profile tool view would "
            "be dead again")
        assert row["tool_name"] == "terminal"


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