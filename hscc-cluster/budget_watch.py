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
Both entry points swallow ALL exceptions and return None — the observer's
"nothing recorded" and the transform's "no opinion". The transform never
returns the input string on a pass-through path: the dispatcher keeps the
FIRST string any plugin returns, so echoing the input would hijack the tool
result away from later plugins (see ``on_transform_tool_result``). A broken
budget watch must never break a worker turn — the same contract the other
workflow hooks honour.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from collections import OrderedDict
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
#: a leak-guard, not a feature). The victim is the LEAST RECENTLY TOUCHED
#: entry — see ``_state_for`` — not the oldest created one.
_MAX_SESSIONS = 128

#: Bound on the fallback board scan in ``_resolve_workspace`` (leak-guard: a
#: hermes home with an unusual number of board dirs is never walked in full).
_MAX_BOARD_DIRS = 64

#: A workspace resolution MISS is retried no more often than this (seconds), so
#: the cold-path scan cannot run per tool result while still recovering a
#: workspace that is created after the first touch.
_WORKSPACE_MISS_RETRY_S = 60.0

_lock = threading.Lock()

#: OrderedDict, not dict: plain dict order is INSERTION order, so evicting the
#: front entry would drop the session that was created first — an idle, stale
#: one — while a session churning new state survived. ``_state_for`` does a
#: move-on-touch (``move_to_end``) so the front entry is genuinely the
#: least-recently-used one. LRU here is only a victim-selection rule for the
#: leak-guard; nothing else in the module depends on the order.
_states: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()

#: task_id -> (workspace, resolved_at_monotonic). Same LRU rule and same bound
#: as ``_states``, but a SEPARATE map: the workspace of a task never moves, so
#: the (potentially expensive) board scan must not be re-paid just because the
#: session's *state* was evicted by the leak-guard. A non-empty resolution is
#: final; an empty one is retried after _WORKSPACE_MISS_RETRY_S.
_workspace_cache: "OrderedDict[str, tuple]" = OrderedDict()

#: Seam for tests: replaced instead of monkeypatching subprocess directly.
_subprocess_run = subprocess.run

#: Seams for tests (same precedent as ``_subprocess_run``): the board scan's
#: two filesystem calls, so a test can count them and pin both the per-task
#: memo (scan at most once) and the ``_MAX_BOARD_DIRS`` bound.
_listdir = os.listdir
_isdir = os.path.isdir


# ------------------------------------------------------------------ plumbing

def _lru_insert(cache: "OrderedDict[str, Any]", key: str, value: Any) -> None:
    """Insert ``key`` as most-recently-used, dropping the LRU entry when full.

    Shared by ``_states`` and ``_workspace_cache`` (both bounded by
    ``_MAX_SESSIONS``). Callers hold ``_lock``.
    """
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > _MAX_SESSIONS:
        cache.popitem(last=False)


def _is_worker_env(environ: Optional[Dict[str, str]] = None) -> str:
    """Return the kanban task id when this process is a kanban worker, else ''."""
    env = environ if environ is not None else os.environ
    return (env.get("HERMES_KANBAN_TASK") or "").strip()


def _resolve_workspace(task_id: str) -> str:
    """One uncached workspace lookup: env vars, then a bounded board scan.

    Order, cheapest first:
      1. HERMES_KANBAN_WORKSPACE (exported into every worker env) — the normal
         hit, no scanning at all.
      2. HERMES_KANBAN_BOARD's own ``workspaces/<task>`` under each board root —
         also exact, no scanning.
      3. A bounded scan of the remaining board dirs: at most ``_MAX_BOARD_DIRS``
         per board root, in SORTED name order.

    Sorting matters for correctness, not tidiness: ``os.listdir`` returns an
    arbitrary (filesystem) order, so an unsorted bound would silently drop a
    resolvable task depending on nothing but how the directory happened to be
    laid out. A deterministic order makes the bound mean "the first N board
    names" and makes it testable. Cheap directory checks only — never opens
    kanban.db (hermes owns that state).
    """
    ws = (os.environ.get("HERMES_KANBAN_WORKSPACE") or "").strip()
    if ws and _isdir(ws):
        return ws
    home = (os.environ.get("HERMES_HOME") or "").strip() or os.path.expanduser("~/.hermes")
    boards_roots = [os.path.join(home, "kanban", "boards")]
    parent = os.path.dirname(home)
    if os.path.basename(parent) == "profiles":  # profile-scoped HERMES_HOME
        boards_roots.append(os.path.join(os.path.dirname(parent), "kanban", "boards"))
    active_board = (os.environ.get("HERMES_KANBAN_BOARD") or "").strip()
    if active_board:
        for boards in boards_roots:
            cand = os.path.join(boards, active_board, "workspaces", task_id)
            if _isdir(cand):
                return cand
    for boards in boards_roots:
        try:
            board_names = sorted(_listdir(boards))
        except OSError:
            continue
        for board in board_names[:_MAX_BOARD_DIRS]:
            cand = os.path.join(boards, board, "workspaces", task_id)
            if _isdir(cand):
                return cand
    return ""


def _workspace_for_task(task_id: str) -> str:
    """Workspace dir for a kanban task id; '' when not resolvable.

    Memoised per task_id in ``_workspace_cache`` so the board scan in
    ``_resolve_workspace`` runs at most once per task per process — a task's
    workspace never moves, so it must not be re-paid when the session *state*
    was evicted by the leak-guard (the old code resolved inside ``_state_for``
    only, so an eviction silently re-triggered a full cold scan).

    A miss (``''``) is retried after ``_WORKSPACE_MISS_RETRY_S`` rather than
    cached forever: the workspace can legitimately be created a moment after
    the first API call, and a miss costs a directory scan, not correctness.
    Callers hold ``_lock``.
    """
    now = time.monotonic()
    hit = _workspace_cache.get(task_id)
    if hit is not None:
        ws, resolved_at = hit
        if ws or (now - resolved_at) < _WORKSPACE_MISS_RETRY_S:
            _workspace_cache.move_to_end(task_id)  # LRU touch
            return ws
    ws = _resolve_workspace(task_id)
    _lru_insert(_workspace_cache, task_id, (ws, now))
    return ws


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
    if st is not None:
        _states.move_to_end(session_id)  # LRU touch: this session is now MRU
        return st
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
    _lru_insert(_states, session_id, st)  # evicts the LEAST recently used
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


def on_transform_tool_result(result: Any, **kwargs: Any) -> Optional[str]:
    """transform_tool_result: append the [budget] line to the next tool result.

    Returns the extended string when (and ONLY when) a note is due. Every other
    path returns ``None`` — meaning "no opinion" — and never the input string.

    Why None and not ``result``: the dispatch site (``model_tools.py``,
    ``_apply_transform_tool_result_hook``) resolves the hook with
    ``next((r for r in hook_results if isinstance(r, str)), result)`` — the
    FIRST non-empty string any plugin returns wins and becomes the tool result.
    Returning the unchanged input would therefore register budget_watch as the
    winning transform: the result would read the same, but any LATER plugin's
    real transform would be silently suppressed. ``None`` is filtered out of
    ``hook_results``, so the pipeline stays open for other plugins. Never raises.
    """
    try:
        task_id = _is_worker_env()
        if not task_id:
            return None  # non-worker: no opinion
        if not isinstance(result, str):
            return None  # not a string we could append to: no opinion
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
                return None  # not armed: no opinion
            calls = st["result_calls"]
            if calls - st["last_note_result"] < NOTE_EVERY_N_CALLS:
                return None  # note due but inside the throttle window: no opinion

            note = _note_text(st["prompt_tokens"], st["api_calls"])
            st["armed"] = False
            st["last_note_result"] = calls
            # Exactly one appended line: normalise a single trailing newline
            # so the note is the last (and only new) line of the block.
            return result.rstrip("\n") + "\n" + note
    except Exception:  # noqa: BLE001 — fail-open, a hook must never break a turn
        return None  # fail-open = no opinion, NOT the input string (see docstring)


# ------------------------------------------------- diagnostics / test helpers

def reset_state() -> None:
    """Drop all per-session state and workspace memos. Tests/doctor only."""
    with _lock:
        _states.clear()
        _workspace_cache.clear()


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
                          "budget hooks (budget_watch.py missing or register() "
                          f"not wired) in: {', '.join(candidates)}",
                "fix": "Re-deploy hscc-cluster from the repo (bootstrap) so "
                       "budget_watch.py and its register() wiring ship."}
    except Exception as exc:  # noqa: BLE001 — doctor rows never crash the run
        return {"name": name, "ok": False, "fatal": False,
                "detail": f"budget hook check errored: {exc}",
                "fix": "Re-run bootstrap payload install for hscc-cluster."}
