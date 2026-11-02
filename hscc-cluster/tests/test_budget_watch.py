"""Tests for hscc-cluster/budget_watch.py (t_6c4f38f2 — the [budget] note).

Fake payloads only — no live model, no real git: the git read is intercepted
through the module's own ``_subprocess_run`` seam, and workspaces are tmp dirs
with a fake ``.git/HEAD`` so HEAD-mtime observation works without a repo.
"""
import os
import time
from pathlib import Path
from types import SimpleNamespace

import budget_watch


# ------------------------------------------------------------------ fixtures

class _FakeRun:
    """Stands in for subprocess.run: answers `git log -1 --format=%ct`."""

    def __init__(self):
        self.ct = 1000
        self.calls = 0

    def __call__(self, argv, **kwargs):
        self.calls += 1
        return SimpleNamespace(returncode=0, stdout=f"{self.ct}\n", stderr="")


def _worker_env(monkeypatch, tmp_path, task="t_budgtest"):
    """Put the process in kanban-worker mode against a tmp workspace."""
    ws = tmp_path / "ws"
    (ws / ".git").mkdir(parents=True)
    (ws / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_KANBAN_TASK", task)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(ws))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    fake = _FakeRun()
    monkeypatch.setattr(budget_watch, "_subprocess_run", fake)
    # No stat throttling in tests — commit observation must be immediate.
    monkeypatch.setattr(budget_watch, "_COMMIT_STAT_MIN_INTERVAL_S", 0.0)
    budget_watch.reset_state()
    monkeypatch.setattr(budget_watch, "reset_state", budget_watch.reset_state,
                        raising=False)
    return ws, fake


def _api(usage_tokens, session="sess-1"):
    budget_watch.on_post_api_request(
        session_id=session, task_id="t_budgtest", api_call_count=1,
        usage={"prompt_tokens": usage_tokens, "completion_tokens": 10})


def _tool(result="tool output text", session="sess-1"):
    return budget_watch.on_transform_tool_result(
        result, tool_name="terminal", args={}, session_id=session)


# ------------------------------------------------------------------- tests

def test_non_worker_sessions_never_noted(monkeypatch, tmp_path):
    """No HERMES_KANBAN_TASK => nothing stored, results pass through untouched."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    budget_watch.reset_state()
    _api(500_000)
    out = _tool("plain result")
    assert out == "plain result"
    assert budget_watch.snapshot("sess-1") == {}


def test_token_threshold_fires_one_note(monkeypatch, tmp_path):
    """prompt_tokens >= 120000 arms; exactly one line appended, then disarmed."""
    _worker_env(monkeypatch, tmp_path)
    _api(125_000)
    out = _tool("first result")
    assert out.startswith("first result")
    lines = out.splitlines()
    assert len(lines) == 2
    assert lines[1] == budget_watch.NOTE_TEMPLATE.format(ctx=125, calls=1)
    assert lines[1].startswith("[budget] context 125K / 1 calls since last commit")
    # Disarmed: a second result comes back untouched.
    assert _tool("second result") == "second result"


def test_note_exactly_one_line_even_with_trailing_newline(monkeypatch, tmp_path):
    """A trailing newline on the result never produces a blank note line."""
    _worker_env(monkeypatch, tmp_path)
    _api(150_000)
    out = _tool("body text\n")
    assert out.endswith(budget_watch.NOTE_TEMPLATE.format(ctx=150, calls=1))
    assert "\n\n" not in out
    assert len(out.splitlines()) == 2


def test_below_threshold_never_notes(monkeypatch, tmp_path):
    _worker_env(monkeypatch, tmp_path)
    for _ in range(39):
        _api(50_000)
    assert _tool("still fine") == "still fine"
    assert budget_watch.snapshot("sess-1")["armed"] is False


def test_calls_threshold_fires_at_40(monkeypatch, tmp_path):
    """>= 40 api calls since the last observed commit arms even at low tokens."""
    _worker_env(monkeypatch, tmp_path)
    for _ in range(39):
        _api(1_000)
    assert _tool("fine") == "fine"
    _api(1_000)  # 40th
    out = _tool("long session")
    assert "[budget]" in out
    assert "40 calls since last commit" in out


def test_rearm_after_new_commit_resets_clock(monkeypatch, tmp_path):
    """A new commit observed in the worktree resets the counter and re-arms."""
    ws, fake = _worker_env(monkeypatch, tmp_path)
    # Baseline commit 1000 observed on the first tool result.
    _api(120_000)
    out = _tool("result one")
    assert "[budget] context 120K / 1 calls" in out

    # The worker commits: HEAD mtime moves, git reports a newer commit time.
    fake.ct = 2000
    new_mtime = (ws / ".git" / "HEAD").stat().st_mtime + 10
    os.utime(ws / ".git" / "HEAD", (new_mtime, new_mtime))
    # One unarmed result lets the (unthrottled) stat observe the commit.
    assert _tool("result two") == "result two"
    st = budget_watch.snapshot("sess-1")
    assert st["api_calls"] == 0 and st["base_ct"] == 2000

    # Burn the 10-call inter-note throttle from the first note.
    for i in range(9):
        assert _tool(f"filler {i}") == f"filler {i}"
    # Re-arm on tokens: the counter counts from the NEW commit.
    _api(130_000)
    out = _tool("after commit")
    assert "[budget] context 130K / 1 calls since last commit" in out


def test_throttle_max_one_note_per_10_results(monkeypatch, tmp_path):
    """Re-arming inside the 10-result window must not emit a second note."""
    _worker_env(monkeypatch, tmp_path)
    _api(120_000)
    assert "[budget]" in _tool("note now")
    # Re-arm immediately, then fewer than 10 results => throttled.
    _api(120_000)
    for i in range(9):
        assert _tool(f"r{i}") == f"r{i}"
    assert budget_watch.snapshot("sess-1")["armed"] is True
    # The 10th result after the last note releases the note.
    out = _tool("r10")
    assert "[budget]" in out
    assert out.startswith("r10")


def test_state_is_per_session(monkeypatch, tmp_path):
    _worker_env(monkeypatch, tmp_path)
    _api(120_000, session="a")
    assert _tool("a out", session="a") != "a out"
    # Session b is untouched by a's arming.
    assert _tool("b out", session="b") == "b out"


def test_fail_open_transform_returns_result(monkeypatch, tmp_path):
    """Any internal explosion => the tool result passes through unchanged."""
    _worker_env(monkeypatch, tmp_path)
    _api(120_000)

    def boom(*a, **k):
        raise RuntimeError("hook internals on fire")

    monkeypatch.setattr(budget_watch, "_note_text", boom)
    assert _tool("must survive") == "must survive"


def test_fail_open_observer_never_raises(monkeypatch, tmp_path):
    """post_api_request swallows junk payloads (usage missing/malformed)."""
    _worker_env(monkeypatch, tmp_path)
    budget_watch.on_post_api_request(session_id="s", usage="not-a-dict")
    budget_watch.on_post_api_request(session_id="s", usage={"prompt_tokens": None})
    budget_watch.on_post_api_request(session_id="s")
    budget_watch.on_post_api_request(session_id="s", usage={"prompt_tokens": "abc"})
    # Still functional afterwards:
    _api(120_000, session="s")
    assert "[budget]" in _tool("ok", session="s")


def test_non_string_result_passes_through(monkeypatch, tmp_path):
    """The first-string-wins contract means non-str results must not be touched."""
    _worker_env(monkeypatch, tmp_path)
    _api(120_000)
    payload = {"output": "dict result"}
    assert budget_watch.on_transform_tool_result(payload, session_id="sess-1") is payload


def test_git_read_goes_through_seam_only_on_head_change(monkeypatch, tmp_path):
    """No subprocess storm: repeated results with HEAD unchanged pay git once."""
    ws, fake = _worker_env(monkeypatch, tmp_path)
    for i in range(5):
        _tool(f"r{i}")
    assert fake.calls == 1
    new_mtime = (ws / ".git" / "HEAD").stat().st_mtime + 5
    os.utime(ws / ".git" / "HEAD", (new_mtime, new_mtime))
    _tool("after commit")
    assert fake.calls == 2


def test_register_wires_both_budget_hooks():
    """Source-text contract (hyphenated dir is not importable as a package):
    register() must wire post_api_request + transform_tool_result to
    budget_watch, matching the existing workflow-hook precedent shape."""
    init_src = (Path(budget_watch.__file__).parent / "__init__.py").read_text()
    assert "from . import budget_watch" in init_src
    assert 'register_hook("post_api_request", budget_watch.on_post_api_request)' in init_src
    assert ('register_hook("transform_tool_result", '
            'budget_watch.on_transform_tool_result)') in init_src
