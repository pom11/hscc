"""Worker budget watch — the [budget] note trigger (t_6c4f38f2, ships 2.15.0).

The worker SOUL promises a "[budget]" note; until now nothing fired it. This
module is the real trigger, wired through two Hermes plugin hooks
(``hermes_cli/plugins.py`` VALID_HOOKS — no new seam):

  post_api_request    observer. For kanban WORKER sessions only (env
                      HERMES_KANBAN_TASK set), records usage.prompt_tokens per
                      session and counts API calls since the last commit seen
                      in the task workspace. Arms the note when
                      prompt_tokens >= TOKEN_THRESHOLD (120K) OR the call count
                      since that commit reaches CALLS_THRESHOLD (40).

  transform_tool_result   while armed (and at most once per
                      NOTE_EVERY_N_CALLS tool results), appends EXACTLY ONE
                      line to the next tool result and disarms.

Where the commit clock is read
------------------------------
Once per tool result, at most one ``os.stat`` of the worktree's HEAD per
_COMMIT_STAT_MIN_INTERVAL_S per workspace — the git subprocess runs only when
that mtime actually changed (an actual commit), never per call. The stat
lives in transform_tool_result because that hook is the one guaranteed to
fire inside the worker's own tool loop; post_api_request stays O(1) and
touches no I/O at all.

Re-arm semantics
----------------
A new commit (HEAD's commit time advanced past the baseline captured when the
note last saw it) resets the since-commit call counter and disarms a pending
note ("commit what is green" — once committed, the stale note is dropped).
The note can fire again once a threshold is crossed anew, subject to the
one-note-per-10-calls throttle.

Fail-open contract
------------------
Both entry points swallow ALL exceptions and return the input unchanged
(transform) or None (observer). A broken budget watch must never break a
worker turn — the same contract the other workflow hooks honour.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from typing import Any, Dict, Optional

# ----------------------------------------------------------------- thresholds
TOKEN_THRESHOLD = 120_000        # prompt_tokens that arm the note
CALLS_THRESHOLD = 40             # API calls since last commit that arm it
NOTE_EVERY_N_CALLS = 10          # max one note per this many tool results
_COMMIT_STAT_MIN_INTERVAL_S = 2.0  # bounds the HEAD stat rate
_GIT_TIMEOUT_S = 3               # subprocess timeout for the (rare) git read

#: Exact note template (worker-SOUL contract). ``{ctx}`` -> context in
#: thousands, ``{calls}`` -> API calls since the last observed commit.
NOTE_TEMPLATE = ("[budget] context {ctx}K / {calls} calls since last commit — "
                 "commit what is green, then complete the card with a "
                 "DONE/LEFT handoff.")

#: Bound on per-session state (one entry per live worker session; eviction is
#: a leak-guard, not a feature).
_MAX_SESSIONS = 128

_lock = threading.Lock()
_states: Dict[str, Dict[str, Any]] = {}

#: Seam for tests: replaced instead of monkeypatching subprocess directly.
_subprocess_run = subprocess.run


# ------------------------------------------------------------------ plumbing

def _is_worker_env(environ: Optional[Dict[str, str]] = None) -> str:
    """Return the kanban task id when this process is a kanban worker, else ''."""
    env = environ if environ is not None else os.environ
    return (env.get("HERMES_KANBAN_TASK") or "").strip()


def _workspace_for_task(task_id: str) -> str:
    """Workspace dir for a kanban task id; '' when not resolvable.

    Prefers HERMES_KANBAN_WORKSPACE (exported into every worker env), then the
    standard scratch layout ``<board-root>/workspaces/<task>``. Cheap directory
    checks only — never opens kanban.db (hermes owns that state).
    """
    ws = (os.environ.get("HERMES_KANBAN_WORKSPACE") or "").strip()
    if ws and os.path.isdir(ws):
        return ws
    home = (os.environ.get("HERMES_HOME") or "").strip() or os.path.expanduser("~/.hermes")
    boards_roots = [os.path.join(home, "kanban", "boards")]
    parent = os.path.dirname(home)
    if os.path.basename(parent) == "profiles":  # profile-scoped HERMES_HOME
        boards_roots.append(os.path.join(os.path.dirname(parent), "kanban", "boards"))
    for boards in boards_roots:
        try:
            for board in os.listdir(boards):
                cand = os.path.join(boards, board, "workspaces", task_id)
                if os.path.isdir(cand):
                    return cand
        except OSError:
            continue
    return ""


def _head_file(workspace: str) -> str:
    """Path of HEAD for a workspace, following the worktree ``gitdir:`` pointer."""
    git_path = os.path.join(workspace, ".git")
    if os.path.isfile(git_path):  # linked worktree
        try:
            with open(git_path, "r", encoding="utf-8") as fh:
                line = fh.read().strip()
            if line.startswith("gitdir:"):
                return os.path.join(line.split(":", 1)[1].strip(), "HEAD")
        except OSError:
            pass
    return os.path.join(git_path, "HEAD")


def _commit_ts(workspace: str) -> Optional[int]:
    """``git log -1 --format=%ct`` for a workspace.

    Only ever called when the HEAD file's mtime moved (see
    ``_observed_commit_ts``), so the subprocess runs once per real commit, not
    per call. ``_subprocess_run`` is the test seam.
    """
    if not workspace:
        return None
    try:
        r = _subprocess_run(
            ["git", "-C", workspace, "log", "-1", "--format=%ct"],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT_S)
        out = (getattr(r, "stdout", "") or "").strip()
        if getattr(r, "returncode", 1) == 0 and out.lstrip("-").isdigit():
            return int(out)
    except Exception:  # noqa: BLE001 — fail-open: unknown commit time, never raise
        return None
    return None


def _observed_commit_ts(st: Dict[str, Any], now: float) -> Optional[int]:
    """Current HEAD commit-time of the session's workspace, stat-throttled.

    One os.stat per _COMMIT_STAT_MIN_INTERVAL_S per workspace; returns the
    last observed value between windows. No git subprocess unless HEAD's mtime
    changed (an actual commit/rebase happened) since the last stat.
    """
    ws = st.get("workspace") or ""
    if not ws:
        return None
    if (now - st.get("last_stat_at", 0.0)) < _COMMIT_STAT_MIN_INTERVAL_S and "head_ct" in st:
        return st["head_ct"]
    st["last_stat_at"] = now
    try:
        mtime_ns = int(os.stat(_head_file(ws)).st_mtime_ns)
    except OSError:
        st["head_ct"] = None
        return None
    if mtime_ns == st.get("head_mtime_ns"):
        return st.get("head_ct")  # HEAD untouched since last stat: no git call
    st["head_mtime_ns"] = mtime_ns
    st["head_ct"] = _commit_ts(ws)
    return st["head_ct"]


def _state_for(session_id: str, task_id: str) -> Dict[str, Any]:
    st = _states.get(session_id)
    if st is None:
        if len(_states) >= _MAX_SESSIONS:  # leak-guard: evict oldest
            try:
                _states.pop(next(iter(_states)), None)
            except StopIteration:
                pass
        st = {
            "task_id": task_id,
            "workspace": _workspace_for_task(task_id),
            "prompt_tokens": 0,
            "api_calls": 0,          # since the baseline commit
            "base_ct": None,         # commit ts the counter is based on
            "last_seen_ct": None,    # last observed HEAD commit ts
            "armed": False,
            "result_calls": 0,
            "last_note_result": -(10 ** 9),
            "head_ct": None,
            "head_mtime_ns": None,
            "last_stat_at": 0.0,
        }
        _states[session_id] = st
    return st


def _note_text(prompt_tokens: int, calls: int) -> str:
    try:
        ctx_k = max(0, int(round(int(prompt_tokens) / 1000)))
    except (TypeError, ValueError):
        ctx_k = 0
    return NOTE_TEMPLATE.format(ctx=ctx_k, calls=max(0, int(calls)))


# --------------------------------------------------------- hook entry points

def on_post_api_request(**kwargs: Any) -> None:
    """post_api_request observer: record prompt_tokens per worker session.

    Non-worker processes (env HERMES_KANBAN_TASK unset) return immediately —
    nothing is even stored for them. Never raises.
    """
    try:
        task_id = _is_worker_env()
        if not task_id:
            return None
        session_id = str(kwargs.get("session_id") or "") or "_unknown_"
        usage = kwargs.get("usage")
        prompt_tokens = 0
        if isinstance(usage, dict):
            raw = usage.get("prompt_tokens")
            if raw is None:  # some payloads normalise to input_tokens
                raw = usage.get("input_tokens")
            try:
                prompt_tokens = int(raw or 0)
            except (TypeError, ValueError):
                prompt_tokens = 0
        with _lock:
            st = _state_for(session_id, task_id)
            st["prompt_tokens"] = prompt_tokens
            st["api_calls"] += 1
            if prompt_tokens >= TOKEN_THRESHOLD or st["api_calls"] >= CALLS_THRESHOLD:
                st["armed"] = True
    except Exception:  # noqa: BLE001 — fail-open, a hook must never break a turn
        return None


def on_transform_tool_result(result: Any, **kwargs: Any) -> Any:
    """transform_tool_result: append the [budget] line to the next tool result.

    Returns the extended string when (and only when) a note is due; anything
    else returns the input untouched. First non-None string wins upstream, so
    a returned str here replaces the tool result — hence the exact append and
    the pass-through of every non-note path. Never raises.
    """
    try:
        task_id = _is_worker_env()
        if not task_id:
            return result
        if not isinstance(result, str):
            return result
        session_id = str(kwargs.get("session_id") or "") or "_unknown_"
        now = time.monotonic()
        with _lock:
            st = _state_for(session_id, task_id)
            st["result_calls"] += 1

            # Commit watch (throttled stat). First observation only sets the
            # baseline; a LATER advance is a real commit during this session.
            head_ct = _observed_commit_ts(st, now)
            if head_ct is not None:
                if st["base_ct"] is None:
                    st["base_ct"] = head_ct
                elif head_ct > st["base_ct"]:
                    # Worker committed: reset the clock and drop any pending
                    # (now stale) note; re-arms on the next threshold crossing.
                    st["api_calls"] = 0
                    st["base_ct"] = head_ct
                    st["armed"] = False

            if not st.get("armed"):
                return result
            calls = st["result_calls"]
            if calls - st["last_note_result"] < NOTE_EVERY_N_CALLS:
                return result

            note = _note_text(st["prompt_tokens"], st["api_calls"])
            st["armed"] = False
            st["last_note_result"] = calls
            # Exactly one appended line: normalise a single trailing newline
            # so the note is the last (and only new) line of the block.
            return result.rstrip("\n") + "\n" + note
    except Exception:  # noqa: BLE001 — fail-open, a hook must never break a turn
        return result


# ------------------------------------------------- diagnostics / test helpers

def reset_state() -> None:
    """Drop all per-session state. Tests and doctor diagnostics only."""
    with _lock:
        _states.clear()


def snapshot(session_id: str) -> Dict[str, Any]:
    """Copy of one session's tracking state (diagnostics/tests); {} if absent."""
    with _lock:
        st = _states.get(session_id)
        return dict(st) if st else {}


#: Doctor row name (fallback when the module itself cannot be imported).
DOCTOR_CHECK_NAME = "worker budget hook ([budget] note)"


def doctor_check(plugins_dirs=None) -> dict:
    """Check the DEPLOYED hscc-cluster wires both budget hooks.

    Verdict lives with the feature (same rule as orch_waker/cron_watchers);
    hscc-bootstrap/doctor.py only adapts the dict to its five Check fields.
    ``plugins_dirs`` is an explicit list of candidate plugin roots (tests);
    production callers pass the hermes-home-derived roots. Non-fatal always:
    a missing hook costs the budget note, never an install.
    """
    name = DOCTOR_CHECK_NAME
    try:
        dirs = list(plugins_dirs or [])
        candidates = []
        for root in dirs:
            cand = os.path.join(str(root), "hscc-cluster")
            if os.path.isdir(cand):
                candidates.append(cand)
        if not candidates:
            return {"name": name, "ok": False, "fatal": False,
                    "detail": "hscc-cluster plugin dir not found under the checked "
                              f"plugin roots ({', '.join(str(d) for d in dirs) or 'none given'})",
                    "fix": "Deploy the hscc-cluster plugin (re-run hscc-bootstrap)."}
        here = os.path.dirname(os.path.abspath(__file__))
        wired = []
        for cand in candidates:
            init_py = os.path.join(cand, "__init__.py")
            bw_py = os.path.join(cand, "budget_watch.py")
            try:
                src = open(init_py, "r", encoding="utf-8").read()
                has_bw = os.path.exists(bw_py)
            except OSError:
                continue
            if has_bw and 'register_hook("post_api_request"' in src \
                    and 'register_hook("transform_tool_result"' in src:
                wired.append(cand)
        if wired:
            return {"name": name, "ok": True, "fatal": False,
                    "detail": f"post_api_request + transform_tool_result wired in "
                              f"{os.path.dirname(wired[0])} (this module: "
                              f"{os.path.samefile(here, wired[0]) and 'same tree' or 'repo copy'})",
                    "fix": ""}
        return {"name": name, "ok": False, "fatal": False,
                "detail": "hscc-cluster is deployed but does not register the "
                          "budget hooks (budget_watch.py missing or register() not wired)",
                "fix": "Re-deploy hscc-cluster from the repo (bootstrap) so "
                       "budget_watch.py and its register() wiring ship."}
    except Exception as exc:  # noqa: BLE001 — doctor rows never crash the run
        return {"name": name, "ok": False, "fatal": False,
                "detail": f"budget hook check errored: {exc}",
                "fix": "Re-run bootstrap payload install for hscc-cluster."}
