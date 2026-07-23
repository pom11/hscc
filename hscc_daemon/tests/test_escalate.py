"""Unit tests for escalate.py - failure-escalation decision logic.

Tests are fully isolated: pure functions, no I/O or subprocess calls.
"""
import pytest
from hscc_daemon.escalate import classify_failure, decide_escalation


# ---------------------------------------------------------------------------
# classify_failure
# ---------------------------------------------------------------------------

class TestClassifyFailure:
    """classify_failure maps error text to a category."""

    def test_none_returns_other(self):
        assert classify_failure(None) == "other"

    def test_empty_string_returns_other(self):
        assert classify_failure("") == "other"

    def test_garbage_returns_other(self):
        assert classify_failure("something unexpected happened") == "other"

    # timeout
    def test_timeout_lowercase(self):
        assert classify_failure("Task timed out after 300s") == "timeout"

    def test_timeout_uppercase(self):
        assert classify_failure("TIMEOUT exceeded") == "timeout"

    def test_timed_out_phrase(self):
        assert classify_failure("Worker timed out while running") == "timeout"

    # oom
    def test_oom_acronym(self):
        assert classify_failure("Killed (OOM)") == "oom"

    def test_out_of_memory(self):
        assert classify_failure("Out of memory error") == "oom"

    def test_killed(self):
        assert classify_failure("Process was killed by signal 9") == "oom"

    def test_memory_keyword(self):
        assert classify_failure("Cannot allocate memory") == "oom"

    # test-failure
    def test_test_keyword(self):
        assert classify_failure("test_something failed") == "test-failure"

    def test_assert_keyword(self):
        assert classify_failure("AssertionError: expected True") == "test-failure"

    def test_pytest_keyword(self):
        assert classify_failure("pytest exit code 1") == "test-failure"

    def test_failed_keyword(self):
        assert classify_failure("Build failed with errors") == "test-failure"

    # tooling
    def test_importerror(self):
        assert classify_failure("ImportError: No module named foo") == "tooling"

    def test_modulenotfound(self):
        assert classify_failure("ModuleNotFoundError: No module named 'bar'") == "tooling"

    def test_traceback(self):
        assert classify_failure("Traceback (most recent call last): ...") == "tooling"


# ---------------------------------------------------------------------------
# decide_escalation
# ---------------------------------------------------------------------------

class TestDecideEscalation:
    """decide_escalation chooses the correct escalation action."""

    # --- below threshold ---
    def test_below_limit(self):
        task = {
            "id": "t-1",
            "assignee": "writer",
            "consecutive_failures": 2,
            "last_failure_error": "oops",
        }
        assert decide_escalation(task) == {"action": "none"}

    def test_zero_failures(self):
        task = {"id": "t-1", "assignee": "writer", "consecutive_failures": 0}
        assert decide_escalation(task) == {"action": "none"}

    # --- at threshold, non-strong assignee ---
    def test_at_limit_non_strong(self):
        task = {
            "id": "t-2",
            "assignee": "writer",
            "consecutive_failures": 3,
            "last_failure_error": "timeout",
        }
        result = decide_escalation(task)
        assert result["action"] == "escalate"
        assert result["reassign_to"] == "architect"
        assert result["category"] == "timeout"
        assert "3 consecutive failures on writer" in result["reason"]

    def test_above_limit_non_strong(self):
        task = {
            "id": "t-3",
            "assignee": "coder",
            "consecutive_failures": 5,
            "last_failure_error": "ImportError: missing thing",
        }
        result = decide_escalation(task)
        assert result["action"] == "escalate"
        assert result["reassign_to"] == "architect"
        assert result["category"] == "tooling"
        assert "5 consecutive failures" in result["reason"]

    # --- at threshold, strong assignee ---
    def test_at_limit_strong_profile(self):
        task = {
            "id": "t-4",
            "assignee": "architect",
            "consecutive_failures": 3,
            "last_failure_error": "out of memory",
        }
        result = decide_escalation(task)
        assert result["action"] == "human"
        assert result["category"] == "oom"
        assert "architect" in result["reason"]
        assert "needs a human" in result["reason"]

    def test_above_limit_strong_profile(self):
        task = {
            "id": "t-5",
            "assignee": "architect",
            "consecutive_failures": 7,
            "last_failure_error": "assertion failed",
        }
        result = decide_escalation(task)
        assert result["action"] == "human"
        assert result["category"] == "test-failure"
        assert "7 attempts" in result["reason"]

    # --- custom fail_limit ---
    def test_custom_fail_limit(self):
        task = {
            "id": "t-6",
            "assignee": "writer",
            "consecutive_failures": 2,
            "last_failure_error": "oops",
        }
        assert decide_escalation(task, fail_limit=2)["action"] == "escalate"
        assert decide_escalation(task, fail_limit=5)["action"] == "none"

    # --- custom strong_profile ---
    def test_custom_strong_profile(self):
        task = {
            "id": "t-7",
            "assignee": "senior-dev",
            "consecutive_failures": 3,
            "last_failure_error": "boom",
        }
        result = decide_escalation(task, strong_profile="senior-dev")
        assert result["action"] == "human"
        assert "senior-dev" in result["reason"]

    # --- missing-key robustness ---
    def test_missing_consecutive_failures(self):
        task = {"id": "t-8", "assignee": "writer"}
        assert decide_escalation(task) == {"action": "none"}

    def test_missing_assignee(self):
        task = {"id": "t-8", "consecutive_failures": 5, "last_failure_error": "err"}
        result = decide_escalation(task)
        # Empty assignee != "architect" -> escalate
        assert result["action"] == "escalate"

    def test_empty_task_dict(self):
        assert decide_escalation({}) == {"action": "none"}
