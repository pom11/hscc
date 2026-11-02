"""Tests for hscc-cluster/budget_watch.py (t_6c4f38f2 — the [budget] note).

Fake payloads only — no live model, no real git: the git read is intercepted
through the module's own ``_subprocess_run`` seam, and workspaces are tmp dirs
with a fake ``.git/HEAD`` so HEAD-mtime observation works without a repo.

Return contract (t_04c35b37 — the review fix)
--------------------------------------------
``transform_tool_result`` callbacks are resolved by the dispatcher as
``next((r for r in hook_results if isinstance(r, str)), result)`` — the FIRST
string any plugin returns wins and BECOMES the tool result. So a pass-through
path that echoes its input is not a no-op: it wins the dispatch, the result
reads unchanged, and every later plugin's real transform is suppressed.

Therefore exactly ONE path may return a string (the note-appended one) and
every pass-through path must return ``None`` = "no opinion". The tests below
pin that on each of the five pass-through paths — asserting ``is None``, not
equality with the input, so a re-patched ``return result`` goes RED.
"""
import os
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


def _tool_noted(result="tool output text", session="sess-1"):
    """Assert this result DID get the note, and return it narrowed to str."""
    out = _tool(result, session=session)
    assert isinstance(out, str) and "[budget]" in out, f"expected a note, got {out!r}"
    return out


#: What a later plugin in the same hook chain would return as its real transform.
LATER_PLUGIN_RESULT = "<redacted by the next plugin>"


def _resolve_like_dispatcher(ours, original):
    """Reproduce the dispatcher's resolution for our callback's return value.

    Verbatim from ``model_tools._apply_transform_tool_result_hook``: the first
    string in the callback results becomes the tool result; a ``None`` (or any
    non-str) is skipped and a LATER callback's string still wins. Returns the
    value the worker would actually see.
    """
    hook_results = [ours, LATER_PLUGIN_RESULT]
    return next((r for r in hook_results if isinstance(r, str)), original)


def _assert_no_opinion(ours, original, path):
    """A pass-through path must yield None AND must not steal the pipeline.

    Checks both halves of the contract: the literal return is ``None`` (not the
    input, not any string), and feeding it through the real dispatcher
    resolution leaves the later plugin's transform in charge.
    """
    assert ours is None, (
        f"{path}: pass-through returned {ours!r} — must return None. "
        "Returning the input string wins the first-string-wins dispatch and "
        "silently suppresses every later plugin's transform.")
    assert not isinstance(ours, str), f"{path}: pass-through returned a str"
    seen = _resolve_like_dispatcher(ours, original)
    assert seen == LATER_PLUGIN_RESULT, (
        f"{path}: budget_watch hijacked the tool result (worker saw {seen!r} "
        f"instead of the later plugin's {LATER_PLUGIN_RESULT!r})")


# ------------------------------------------------------------------- tests

def test_non_worker_sessions_never_noted(monkeypatch, tmp_path):
    """No HERMES_KANBAN_TASK => nothing stored and the hook has no opinion."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    budget_watch.reset_state()
    _api(500_000)
    out = _tool("plain result")
    _assert_no_opinion(out, "plain result", "non-worker env")
    assert budget_watch.snapshot("sess-1") == {}


def test_token_threshold_fires_one_note(monkeypatch, tmp_path):
    """prompt_tokens >= 120000 arms; exactly one line appended, then disarmed."""
    _worker_env(monkeypatch, tmp_path)
    _api(125_000)
    out = _tool_noted("first result")
    assert out.startswith("first result")
    lines = out.splitlines()
    assert len(lines) == 2
    assert lines[1] == budget_watch.NOTE_TEMPLATE.format(ctx=125, calls=1)
    assert lines[1].startswith("[budget] context 125K / 1 calls since last commit")
    # Disarmed: a second result comes back with no opinion at all.
    _assert_no_opinion(_tool("second result"), "second result", "armed->disarmed")


def test_note_exactly_one_line_even_with_trailing_newline(monkeypatch, tmp_path):
    """A trailing newline on the result never produces a blank note line."""
    _worker_env(monkeypatch, tmp_path)
    _api(150_000)
    out = _tool_noted("body text\n")
    assert out.endswith(budget_watch.NOTE_TEMPLATE.format(ctx=150, calls=1))
    assert "\n\n" not in out
    assert len(out.splitlines()) == 2


def test_below_threshold_never_notes(monkeypatch, tmp_path):
    _worker_env(monkeypatch, tmp_path)
    for _ in range(39):
        _api(50_000)
    _assert_no_opinion(_tool("still fine"), "still fine", "below threshold")
    assert budget_watch.snapshot("sess-1")["armed"] is False


def test_calls_threshold_fires_at_40(monkeypatch, tmp_path):
    """>= 40 api calls since the last observed commit arms even at low tokens."""
    _worker_env(monkeypatch, tmp_path)
    for _ in range(39):
        _api(1_000)
    _assert_no_opinion(_tool("fine"), "fine", "39 calls (unarmed)")
    _api(1_000)  # 40th
    out = _tool_noted("long session")
    assert "40 calls since last commit" in out


def test_rearm_after_new_commit_resets_clock(monkeypatch, tmp_path):
    """A new commit observed in the worktree resets the counter and re-arms."""
    ws, fake = _worker_env(monkeypatch, tmp_path)
    # Baseline commit 1000 observed on the first tool result.
    _api(120_000)
    out = _tool_noted("result one")
    assert "[budget] context 120K / 1 calls" in out

    # The worker commits: HEAD mtime moves, git reports a newer commit time.
    fake.ct = 2000
    new_mtime = (ws / ".git" / "HEAD").stat().st_mtime + 10
    os.utime(ws / ".git" / "HEAD", (new_mtime, new_mtime))
    # One unarmed result lets the (unthrottled) stat observe the commit.
    _assert_no_opinion(_tool("result two"), "result two", "post-commit unarmed")
    st = budget_watch.snapshot("sess-1")
    assert st["api_calls"] == 0 and st["base_ct"] == 2000

    # Burn the 10-call inter-note throttle from the first note.
    for i in range(9):
        _assert_no_opinion(_tool(f"filler {i}"), f"filler {i}", f"filler {i}")
    # Re-arm on tokens: the counter counts from the NEW commit.
    _api(130_000)
    out = _tool("after commit")
    assert "[budget] context 130K / 1 calls since last commit" in out


def test_throttle_max_one_note_per_10_results(monkeypatch, tmp_path):
    """Re-arming inside the 10-result window must not emit a second note."""
    _worker_env(monkeypatch, tmp_path)
    _api(120_000)
    assert "[budget]" in _tool_noted("note now")
    # Re-arm immediately, then fewer than 10 results => throttled.
    _api(120_000)
    for i in range(9):
        _assert_no_opinion(_tool(f"r{i}"), f"r{i}", f"throttled r{i}")
    assert budget_watch.snapshot("sess-1")["armed"] is True
    # The 10th result after the last note releases the note.
    out = _tool_noted("r10")
    assert out.startswith("r10")


def test_state_is_per_session(monkeypatch, tmp_path):
    _worker_env(monkeypatch, tmp_path)
    _api(120_000, session="a")
    assert _tool("a out", session="a") != "a out"
    # Session b is untouched by a's arming.
    _assert_no_opinion(_tool("b out", session="b"), "b out", "unarmed session b")


def test_fail_open_transform_returns_none(monkeypatch, tmp_path):
    """Any internal explosion => no opinion (still swallowed, never raises).

    Fail-open means budget_watch abstains, not that it re-publishes the input:
    the swallow semantics are unchanged, only the return value.
    """
    _worker_env(monkeypatch, tmp_path)
    _api(120_000)

    def boom(*a, **k):
        raise RuntimeError("hook internals on fire")

    monkeypatch.setattr(budget_watch, "_note_text", boom)
    out = _tool("must survive")
    _assert_no_opinion(out, "must survive", "fail-open except handler")
    assert budget_watch.snapshot("sess-1") != {}, "state tracking still happened"


def test_fail_open_observer_never_raises(monkeypatch, tmp_path):
    """post_api_request swallows junk payloads (usage missing/malformed)."""
    _worker_env(monkeypatch, tmp_path)
    budget_watch.on_post_api_request(session_id="s", usage="not-a-dict")
    budget_watch.on_post_api_request(session_id="s", usage={"prompt_tokens": None})
    budget_watch.on_post_api_request(session_id="s")
    budget_watch.on_post_api_request(session_id="s", usage={"prompt_tokens": "abc"})
    # Still functional afterwards:
    _api(120_000, session="s")
    assert "[budget]" in _tool_noted("ok", session="s")


def test_non_string_result_returns_none(monkeypatch, tmp_path):
    """Non-str results are abstained on — the dispatcher's fallback keeps them.

    ``next(..., result)`` restores the original object when every callback
    abstains, so returning None is what actually preserves a dict/int result;
    returning it here would have made it a winning transform (and a dict could
    never be appended to in the first place).
    """
    _worker_env(monkeypatch, tmp_path)
    _api(120_000)  # armed, so this exercises the isinstance guard, not arming
    for payload in ({"output": "dict result"}, 42):
        assert budget_watch.on_transform_tool_result(
            payload, session_id="sess-1") is None


# ------------------------- pass-through contract (t_04c35b37 review fix) ----
# One test per pass-through path, each asserting `is None` (not the input, not
# any str) AND that a later plugin's transform survives the dispatch.


def test_pass_through_non_worker_env_returns_none(monkeypatch, tmp_path):
    """(a) Non-worker env: the very first guard must abstain, not echo."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    budget_watch.reset_state()
    out = _tool("gateway-side tool result")
    _assert_no_opinion(out, "gateway-side tool result", "(a) non-worker env")


def test_pass_through_non_string_result_returns_none(monkeypatch, tmp_path):
    """(b) dict/int results: no opinion, and the object still reaches context."""
    _worker_env(monkeypatch, tmp_path)
    _api(120_000)
    for payload in ({"output": "dict result"}, 42, ["a list"], None):
        ours = budget_watch.on_transform_tool_result(payload, session_id="sess-1")
        assert ours is None, f"non-str {type(payload).__name__} returned {ours!r}"
        # The dispatcher's fallback hands the ORIGINAL object back unchanged.
        assert next((r for r in [ours] if isinstance(r, str)), payload) is payload


def test_pass_through_unarmed_returns_none(monkeypatch, tmp_path):
    """(c) armed=False: the early return abstains after still counting results."""
    _worker_env(monkeypatch, tmp_path)
    _api(1_000)  # far below both thresholds
    out = _tool("ordinary output")
    _assert_no_opinion(out, "ordinary output", "(c) not-armed early return")
    # Proof the guard reached the armed check (bookkeeping ran, note not due):
    st = budget_watch.snapshot("sess-1")
    assert st["result_calls"] == 1 and st["armed"] is False


def test_pass_through_throttled_inside_window_returns_none(monkeypatch, tmp_path):
    """(d) note due but < NOTE_EVERY_N_CALLS since the last note: abstain.

    The note stays pending (armed=True) — throttling must hide the note, not
    publish the untouched result as this plugin's contribution.
    """
    _worker_env(monkeypatch, tmp_path)
    _api(120_000)
    assert "[budget]" in _tool("first")  # note fires: last_note_result = 1
    _api(120_000)                        # re-arm immediately
    out = _tool("second")                # 2nd result: inside the 10-call window
    _assert_no_opinion(out, "second", "(d) throttled second hit")
    st = budget_watch.snapshot("sess-1")
    assert st["armed"] is True, "throttle must keep the note pending, not drop it"
    assert st["last_note_result"] == 1


def test_pass_through_never_hijacks_a_later_plugin_transform(monkeypatch, tmp_path):
    """The hazard itself, end to end: an unarmed budget_watch must not decide
    what the worker reads. With the old `return result` the first-string-wins
    dispatch returned budget_watch's echo and the later plugin never ran.
    """
    _worker_env(monkeypatch, tmp_path)
    original = "raw tool output body"
    ours = budget_watch.on_transform_tool_result(original, session_id="sess-1")
    assert ours is None
    assert _resolve_like_dispatcher(ours, original) == LATER_PLUGIN_RESULT
    # Contrast: had this path echoed the input, the worker would read this:
    hijacked = next((r for r in [original, LATER_PLUGIN_RESULT]
                     if isinstance(r, str)), original)
    assert hijacked == original != LATER_PLUGIN_RESULT


def test_note_path_is_the_only_string_return(monkeypatch, tmp_path):
    """Exactly one path returns a str, and it returns EXACTLY one added line."""
    _worker_env(monkeypatch, tmp_path)
    _api(120_000)
    out = _tool("the result")
    assert isinstance(out, str) and "[budget]" in out
    assert out.splitlines()[0] == "the result"
    assert len(out.splitlines()) == 2
    # Dispatcher keeps the note (it is a string) — as it must.
    assert _resolve_like_dispatcher(out, "the result") == out


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


# ------------------- leak-guard eviction order + workspace memo (t_b91f1158) -
#
# (1) The `_MAX_SESSIONS` guard must evict the LEAST RECENTLY USED entry. On a
# plain dict, `next(iter(_states))` is insertion order, i.e. "oldest CREATED":
# a long-lived session that is merely idle would be dropped while a burst of
# fresh sessions survived. These tests pin move-on-touch semantics — a test that
# passes with a plain dict would go RED on the old `pop(next(iter(...)))`.
#
# (2) `_workspace_for_task` must pay the board scan AT MOST ONCE per task,
# including across a state eviction (the old code resolved inside `_state_for`,
# so an eviction re-paid the whole cold scan). Scan traffic is counted through
# the module's own `_listdir` seam, same precedent as `_subprocess_run`.


def _fill_states(n):
    """Create n session states in creation order s0..s<n-1> (unarmed payloads)."""
    for i in range(n):
        budget_watch.on_post_api_request(
            session_id=f"s{i}", usage={"prompt_tokens": 1})


def test_eviction_victim_is_least_recently_used_not_oldest(monkeypatch, tmp_path):
    """Idle-but-old survives if it was touched recently; a stale entry dies."""
    _worker_env(monkeypatch, tmp_path)
    monkeypatch.setattr(budget_watch, "_MAX_SESSIONS", 4)
    _fill_states(4)                                   # s0 s1 s2 s3 (s0 oldest)
    # Touch s0 so it becomes most-recently-used despite being created first.
    _api(1_000, session="s0")
    # New session forces one eviction. Insertion-order eviction would kill s0.
    _api(1_000, session="s4")
    assert budget_watch.snapshot("s0") != {}, "LRU: recently-touched s0 was evicted"
    assert budget_watch.snapshot("s1") == {}, (
        "eviction should have dropped the least-recently-USED entry (s1)")
    for sid in ("s2", "s3", "s4"):
        assert budget_watch.snapshot(sid) != {}, f"{sid} must survive"
    assert len(budget_watch._states) == 4


def test_eviction_repeatedly_spares_the_touched_session(monkeypatch, tmp_path):
    """Churning new sessions must never evict a session that keeps working.

    The old failure mode: the survivor set was "the last N created", so a
    single long-lived worker at the front of the dict was evicted first. It then
    came back on its very next API call with a FRESH zeroed state — so checking
    only that the session "is still there" is not enough (re-creation hides the
    eviction). The counter is what proves continuity: an evicted-and-recreated
    state restarts at 0.
    """
    _worker_env(monkeypatch, tmp_path)
    monkeypatch.setattr(budget_watch, "_MAX_SESSIONS", 3)
    calls = 0
    for i in range(10):                                # a burst of churn
        _api(1_000, session=f"churn{i}")
        _api(1_000, session="long-lived")              # keeps it MRU
        calls += 1
    st = budget_watch.snapshot("long-lived")
    assert st != {}, "an actively-used session was evicted by churn"
    assert st["api_calls"] == calls, (
        f"session state was silently rebuilt mid-run (api_calls={st['api_calls']}, "
        f"expected {calls}) — the leak-guard evicted a session that was still "
        "working and its counters restarted, losing the budget watch")
    assert len(budget_watch._states) == 3


def test_lru_state_is_still_bounded(monkeypatch, tmp_path):
    """No unbounded growth: the guard still caps _states at the configured max."""
    _worker_env(monkeypatch, tmp_path)
    monkeypatch.setattr(budget_watch, "_MAX_SESSIONS", 5)
    for i in range(40):
        _api(1_000, session=f"s{i}")
    assert len(budget_watch._states) == 5


def _fake_home(monkeypatch, tmp_path, task, n_boards=1):
    """HERMES_HOME layout with <home>/kanban/boards/<board>/workspaces/<task>."""
    monkeypatch.delenv("HERMES_KANBAN_WORKSPACE", raising=False)
    home = tmp_path / "home"
    for b in range(n_boards):
        (home / "kanban" / "boards" / f"board{b}" / "workspaces" / task).mkdir(
            parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task)
    budget_watch.reset_state()
    return home


class _CountingListdir:
    def __init__(self, real):
        self._real = real
        self.calls = 0
        self.seen = []

    def __call__(self, path):
        self.calls += 1
        self.seen.append(str(path))
        return self._real(path)


def test_workspace_scan_runs_once_per_task(monkeypatch, tmp_path):
    """Cold env: the board scan is memoised, not re-paid on every touch."""
    task = "t_scanmemo"
    home = _fake_home(monkeypatch, tmp_path, task)
    counter = _CountingListdir(budget_watch._listdir)
    monkeypatch.setattr(budget_watch, "_listdir", counter)

    first = budget_watch._workspace_for_task(task)
    n_after_first = counter.calls
    assert first == str(home / "kanban" / "boards" / "board0" / "workspaces" / task)
    for _ in range(20):
        assert budget_watch._workspace_for_task(task) == first
    assert counter.calls == n_after_first, (
        f"board scan re-ran ({counter.calls} listdir calls for one task) — the "
        "resolved workspace is not memoised")


def test_workspace_scan_not_repaid_after_state_eviction(monkeypatch, tmp_path):
    """The card's cold-path question: state eviction must NOT re-scan.

    The workspace lives in its own memo precisely so the leak-guard evicting a
    session's state cannot make the next API call walk every board dir again.
    """
    task = "t_evictrescan"
    home = _fake_home(monkeypatch, tmp_path, task)
    counter = _CountingListdir(budget_watch._listdir)
    monkeypatch.setattr(budget_watch, "_listdir", counter)

    # Populate the session state (first touch pays the scan once).
    _api(1_000)
    paid = counter.calls
    assert paid >= 1
    # Force the leak-guard to evict the session state entirely.
    monkeypatch.setattr(budget_watch, "_MAX_SESSIONS", 1)
    _api(1_000, session="other-session")
    assert budget_watch.snapshot("sess-1") == {}, "state should have been evicted"
    # Re-create the evicted session's state: workspace must come from the memo.
    _api(1_000)
    assert counter.calls == paid, (
        "eviction re-triggered the cold board scan instead of reusing the "
        f"memoised workspace ({paid} -> {counter.calls} listdir calls)")
    # The evicted-and-recreated state still has the right workspace.
    assert budget_watch.snapshot("sess-1")["workspace"] == str(
        home / "kanban" / "boards" / "board0" / "workspaces" / task)


def test_workspace_miss_is_retried_not_cached_forever(monkeypatch, tmp_path):
    """A miss must not become permanent, but must not scan per call either."""
    task = "t_latews"
    home = tmp_path / "home"
    (home / "kanban" / "boards" / "board0" / "workspaces").mkdir(parents=True)
    monkeypatch.delenv("HERMES_KANBAN_WORKSPACE", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    budget_watch.reset_state()
    counter = _CountingListdir(budget_watch._listdir)
    monkeypatch.setattr(budget_watch, "_listdir", counter)

    assert budget_watch._workspace_for_task(task) == ""
    paid = counter.calls
    assert paid >= 1
    for _ in range(10):                               # inside the retry window
        assert budget_watch._workspace_for_task(task) == ""
    assert counter.calls == paid, f"a miss re-scanned on every call ({paid} -> {counter.calls})"

    # The workspace appears later (worker dir created after the first touch):
    # once the retry window expires the scan runs again and finds it.
    late = home / "kanban" / "boards" / "board0" / "workspaces" / task
    late.mkdir(parents=True)
    monkeypatch.setattr(budget_watch, "_WORKSPACE_MISS_RETRY_S", 0.0)
    assert budget_watch._workspace_for_task(task) == str(late)


def test_board_scan_is_bounded(monkeypatch, tmp_path):
    """The scan itself is bounded: it never walks an unbounded board list."""
    task = "t_deep"
    home = tmp_path / "home"
    boards = home / "kanban" / "boards"
    n = budget_watch._MAX_BOARD_DIRS + 25
    for b in range(n):
        (boards / f"b{b:03d}" / "workspaces").mkdir(parents=True)
    # Put the match LAST so an unbounded scan would find it.
    target = boards / f"b{n - 1:03d}" / "workspaces" / task
    target.mkdir(parents=True)
    monkeypatch.delenv("HERMES_KANBAN_WORKSPACE", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    budget_watch.reset_state()

    counter = _CountingListdir(budget_watch._listdir)
    monkeypatch.setattr(budget_watch, "_listdir", counter)
    got = budget_watch._workspace_for_task(task)
    assert got == "", "scan ran past _MAX_BOARD_DIRS — the bound is not applied"
    assert counter.calls <= budget_watch._MAX_BOARD_DIRS, (
        f"{counter.calls} listdir calls for a scan bounded to "
        f"{budget_watch._MAX_BOARD_DIRS}")

    # A match INSIDE the bound is still found (the bound is not just a failure).
    budget_watch.reset_state()
    near = boards / "b000" / "workspaces" / task
    near.mkdir(parents=True)
    assert budget_watch._workspace_for_task(task) == str(near)


def test_workspace_cache_is_bounded(monkeypatch, tmp_path):
    """The workspace memo cannot grow past the session bound either."""
    home = tmp_path / "home"
    (home / "kanban" / "boards" / "b0" / "workspaces").mkdir(parents=True)
    monkeypatch.delenv("HERMES_KANBAN_WORKSPACE", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    budget_watch.reset_state()
    monkeypatch.setattr(budget_watch, "_MAX_SESSIONS", 4)
    for i in range(30):
        budget_watch._workspace_for_task(f"t_{i}")
    assert len(budget_watch._workspace_cache) == 4

