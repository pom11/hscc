"""Idempotent resume probe for the agentic work-flow (WS4 / G6).

kanban tracks RUN history (attempts) but not WORK-PRODUCT state. Before a
re-dispatched worker redoes a task, `probe_task_state` judges "is this already
satisfied?" from three signals, kanban-truth first:

  1. lifecycle: task already done/review → satisfied (don't re-dispatch)
  2. git work-product: the task branch exists AND its diff vs base touches the
     plan's target files → work is present
  3. tests: if the plan names tests, run them on the branch; green + on-target
     diff → satisfied; else resume from the first unsatisfied checklist item

"done vs abandoned mid-edit" is disambiguated by the last run outcome
(crashed/timed_out/reclaimed) + branch dirtiness.

The probe is READ-ONLY (git status/diff/branch + an opt-in test command). It
augments build_worker_context via a comment — it never patches kanban core.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any

_SATISFIED_STATUSES = {"done", "review"}
_INCOMPLETE_OUTCOMES = {"crashed", "timed_out", "reclaimed", "spawn_failed", "gave_up"}


@dataclass
class ProbeResult:
    satisfied: bool
    resume_from: Optional[int]          # checklist index to resume at, or None
    reason: str
    evidence: Dict[str, Any] = field(default_factory=dict)


def _git(repo: str, *args, timeout: int = 20) -> dict:
    try:
        r = subprocess.run(["git", "-C", repo, *args],
                           capture_output=True, text=True, timeout=timeout)
        return {"ok": r.returncode == 0, "out": r.stdout.strip(),
                "err": r.stderr.strip(), "code": r.returncode}
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as e:
        return {"ok": False, "out": "", "err": str(e), "code": -1}


def branch_exists(repo: str, branch: str) -> bool:
    return _git(repo, "rev-parse", "--verify", f"refs/heads/{branch}")["ok"]


def changed_files(repo: str, base: str, branch: str) -> List[str]:
    """Files changed on `branch` vs `base` (3-dot = since divergence)."""
    r = _git(repo, "diff", "--name-only", f"{base}...{branch}")
    return [f for f in r["out"].splitlines() if f] if r["ok"] else []


def branch_dirty(repo: str) -> bool:
    """True if the working tree has uncommitted changes (mid-edit signal)."""
    r = _git(repo, "status", "--porcelain")
    return bool(r["ok"] and r["out"])


def _targets_hit(changed: List[str], targets: List[str]) -> List[str]:
    """Which target paths/prefixes appear in the changed-file set."""
    hits = []
    for t in targets:
        if any(c == t or c.startswith(t.rstrip("/") + "/") or c.endswith(t)
                for c in changed):
            hits.append(t)
    return hits


def run_tests(repo: str, test_cmd: Optional[List[str]], timeout: int = 600) -> Optional[bool]:
    """Run the plan's test command in the repo. None when no command given."""
    if not test_cmd:
        return None
    try:
        r = subprocess.run(test_cmd, cwd=repo, capture_output=True, text=True,
                           timeout=timeout)
        return r.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def probe_task_state(task: Dict[str, Any], *, repo: str, plan: Dict[str, Any],
                     base: str = "main", _tester=None) -> ProbeResult:
    """Decide whether a task is already satisfied or should resume, and where.

    task: {status, branch_name, last_run_outcome?}  (subset of the kanban row)
    plan: {checklist: [str,...], targets: [path,...], test_cmd: [..]|None}
    """
    # 1. kanban lifecycle is authoritative
    status = (task.get("status") or "").lower()
    if status in _SATISFIED_STATUSES:
        return ProbeResult(True, None, f"task already '{status}' (kanban-authoritative)",
                           {"status": status})

    branch = task.get("branch_name")
    checklist = plan.get("checklist") or []
    targets = plan.get("targets") or []

    # 2. git work-product
    if not branch or not branch_exists(repo, branch):
        return ProbeResult(False, 0, "no task branch yet — start from the top",
                           {"branch": branch, "branch_exists": False})

    changed = changed_files(repo, base, branch)
    hits = _targets_hit(changed, targets) if targets else changed
    dirty = branch_dirty(repo)
    last_outcome = (task.get("last_run_outcome") or "").lower()
    abandoned = dirty or last_outcome in _INCOMPLETE_OUTCOMES

    if not changed:
        return ProbeResult(False, 0, "branch exists but has no committed work — start from the top",
                           {"branch": branch, "changed": []})

    if targets and not hits:
        # work landed, but not on the files this task is about → treat as not-ours
        return ProbeResult(False, 0,
                           "branch has changes but none touch the task's target files",
                           {"changed": changed, "targets": targets})

    # 3. tests
    tester = _tester or run_tests
    tests_ok = tester(repo, plan.get("test_cmd"))

    if tests_ok is True and not abandoned:
        return ProbeResult(True, None,
                           "on-target work committed + tests green + clean → satisfied",
                           {"changed": changed, "hits": hits, "tests": "green"})

    # partial / abandoned / red tests → resume at first unsatisfied checklist item.
    # Heuristic: count checklist items whose named target already shows in the diff.
    resume_idx = 0
    for i, item in enumerate(checklist):
        item_targets = [t for t in targets if t in item] if targets else []
        done = item_targets and _targets_hit(changed, item_targets)
        if done:
            resume_idx = i + 1
        else:
            break
    resume_idx = min(resume_idx, len(checklist)) if checklist else 0
    why = ("tests red — resume" if tests_ok is False else
           "mid-edit/abandoned — resume" if abandoned else
           "partial work — resume")
    return ProbeResult(False, resume_idx, why,
                       {"changed": changed, "hits": hits,
                        "tests": tests_ok, "abandoned": abandoned})


# ── dispatch hook: post an idempotent-resume note on re-dispatch ─────────────

def resume_note(task, *, repo, base="main"):
    """Build a short resume reminder from the task branch's committed work, or
    None when there's nothing committed yet (nothing to resume). Pure read-only
    git; does not run tests (dispatch time must be cheap)."""
    branch = (task or {}).get("branch_name") if isinstance(task, dict) else getattr(task, "branch_name", None)
    if not branch or not branch_exists(repo, branch):
        return None
    changed = changed_files(repo, base, branch)
    if not changed:
        return None
    n = len(changed)
    shown = ", ".join(changed[:8]) + (" …" if n > 8 else "")
    return (
        "♻️ **Resume — prior work exists on this task.**\n"
        f"Branch `{branch}` already has committed changes to {n} file(s): {shown}\n"
        "Read what's there and CONTINUE from the first unfinished step — do NOT "
        "redo files that are already done. Verify with the task's tests."
    )


def on_kanban_task_claimed(task_id=None, board=None, profile_name=None,
                           assignee=None, run_id=None, **kwargs):
    """Hook handler for `kanban_task_claimed` (fires on every task claim).

    Posts a resume note (from the task branch's committed state) as a kanban
    comment, which build_worker_context surfaces to the re-dispatched worker so
    it resumes instead of redoing. Best-effort: never raises (the core fires
    this in a try/except, but we double-guard).

    ``board`` is the board slug passed by the upstream hook — used so the
    comment lands on the SAME board the task lives on. ``repo`` is inferred
    from the task's workspace_path, then cwd.

    NOTE: upstream's ``kanban_task_claimed`` no longer passes the full task
    dict or a pre-opened ``conn``; we fetch them via kanban_db.connect/get_task.
    """
    try:
        import os as _os
        from hermes_cli import kanban_db as _kb
        c = _kb.connect(board=board) if board else _kb.connect()
        try:
            task_row = _kb.get_task(c, task_id)
            t = {
                "branch_name": task_row.get("branch_name") if task_row else None,
                "workspace_path": task_row.get("workspace_path") if task_row else None,
            } if task_row else {}
            work_repo = t.get("workspace_path") or _os.getcwd()
            note = resume_note(t, repo=work_repo)
            if note and task_id:
                # Stamp profile_name into the resume note when available.
                if profile_name:
                    note = f"  `profile`: `{profile_name}`\n\n" + note
                _kb.add_comment(c, task_id, author="hscc-resume", body=note)
        finally:
            c.close()
        return {"posted": bool(note and task_id), "task_id": task_id}
    except Exception:
        return None


# ── kanban_task_blocked handler ──────────────────────────────────────────────

_BLOCKED_LOG = os.path.join(os.path.expanduser("~/.hscc"), "blocked_tasks.jsonl")


def on_kanban_task_blocked(task_id=None, profile_name=None, reason=None,
                           board=None, **kwargs):
    """Hook handler for `kanban_task_blocked` (fires when a task is blocked).

    Surfaces blocked tasks so the ops team sees them:
    1. Posts a concise alert to the HSCC ops Telegram topic via the existing
       ``hscc_daemon.telegram.notify_operations`` path (reused at import time).
    2. Appends a JSON line to ``~/.hscc/blocked_tasks.jsonl`` so the dashboard
       or status tools can read stuck-task history.

    Best-effort: never raises. The ``reason`` field is new in hermes 0.17 —
    it is included verbatim in the alert.

    Parameters
    ----------
    task_id : str or None
        Kanban task identifier.
    profile_name : str or None
        Which profile blocked the task.
    reason : str or None
        Why the task is blocked (new in 0.17).
    board : str or None
        Board slug, passed by the upstream hook.
    """
    try:
        # Build the alert message.
        parts = ["\U0001f6a7 **Task blocked**"]
        if task_id:
            parts.append(f"  `task_id`: `{task_id}`")
        if profile_name:
            parts.append(f"  `profile`: `{profile_name}`")
        if board:
            parts.append(f"  `board`: `{board}`")
        if reason:
            parts.append(f"  **reason**: {reason}")
        if not any(parts[1:]):
            parts = ["\U0001f6a7 **Task blocked** (no context available)"]
        alert = "\n".join(parts)

        # Post to Telegram ops topic (best-effort; may not be importable outside
        # the daemon context).
        try:
            from . import _telegram_compat as _tg
            _tg.notify_operations(alert)
        except ImportError:
            pass

        # Persist to the local log so the dashboard can read it.
        try:
            import datetime
            import json
            entry = {
                "task_id": task_id,
                "profile_name": profile_name,
                "reason": reason,
                "board": board,
                "blocked_at": datetime.datetime.utcnow().isoformat(),
            }
            with open(_BLOCKED_LOG, "a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

        return {"task_id": task_id, "notified": True}
    except Exception:
        return None


# ── kanban_task_completed handler ────────────────────────────────────────────

_COMPLETION_LOG = os.path.join(os.path.expanduser("~/.hscc"), "task_completions.jsonl")


def _try_auto_unblock(child_task_id, parent_task_id):
    """If HSCC's kanban supports dependency linking, attempt to auto-unblock.

    Checks whether the child was blocked *because* of the parent (common in
    HSCC's ``kanban_link`` pattern). If so, moves the child from blocked →
    ready. Best-effort — silently does nothing if the API or schema doesn't
    match.

    Returns True if the child was auto-unblocked, False otherwise.
    """
    try:
        from hermes_cli import kanban_db as _kb
        c = _kb.connect()
        child_row = _kb.get_task(c, child_task_id)
        if not child_row:
            return False

        # If the child is in blocked status and its block reason references
        # the parent, auto-unblock.
        child_status = (child_row.get("status") or "").lower()
        if child_status != "blocked":
            return False

        # Check block comments for parent reference.
        # hermes_cli kanban_db stores comments as a list in the row or separately.
        try:
            comments = _kb.get_comments(c, child_task_id) or []
        except Exception:
            # Some versions may not have get_comments — skip auto-unblock.
            return False

        for comment in comments:
            body = str(comment.get("body", "")) if isinstance(comment, dict) else str(comment)
            if parent_task_id and parent_task_id in body:
                # Auto-unblock: set status to ready, clear claim_lock.
                _kb.update_task(c, child_task_id, {"status": "ready"})
                try:
                    _kb.clear_lock(c, child_task_id)
                except Exception:
                    pass
                return True
        return False
    except Exception:
        return False


def on_kanban_task_completed(task_id=None, profile_name=None, summary=None,
                             board=None, **kwargs):
    """Hook handler for `kanban_task_completed` (fires when a task completes).

    Records the completion for HSCC's own metrics:
    1. Appends a JSON line to ``~/.hscc/task_completions.jsonl`` with
       task_id, profile_name, summary, and timestamp.
    2. Best-effort auto-unblock: scans for any blocked tasks that were waiting
       on this completed task (by matching the parent task_id in block comments)
       and promotes them to ready. If HSCC has no dependency mechanism, this
       silently does nothing — we do NOT build a new dependency system.

    Best-effort: never raises.

    Parameters
    ----------
    task_id : str or None
        Kanban task identifier.
    profile_name : str or None
        Which profile completed the task.
    summary : str or None
        The task completion summary (from the worker's ``kanban_complete`` call).
    board : str or None
        Board slug.
    """
    try:
        import datetime
        import json

        # 1. Record the completion.
        entry = {
            "task_id": task_id,
            "profile_name": profile_name,
            "summary": summary,
            "board": board,
            "completed_at": datetime.datetime.utcnow().isoformat(),
        }
        try:
            with open(_COMPLETION_LOG, "a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

        # 2. Best-effort auto-unblock of dependents.
        # Read the blocked log to find tasks that reference this completed task.
        try:
            if os.path.exists(_BLOCKED_LOG):
                with open(_BLOCKED_LOG) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            blocked_entry = json.loads(line)
                            blocked_tid = blocked_entry.get("task_id")
                            if blocked_tid and blocked_tid != task_id:
                                _try_auto_unblock(blocked_tid, task_id)
                        except (json.JSONDecodeError, KeyError):
                            continue
        except Exception:
            pass

        return {"task_id": task_id, "completed": True}
    except Exception:
        return None


# ── pre_tool_call observability hook ──────────────────────────────────────

_TOOL_EVENT_LOG = os.path.join(os.path.expanduser("~/.hscc"), "tool_events.jsonl")


def on_pre_tool_call(**kwargs):
    """Stamp profile_name into HSCC observability for every tool call."""
    try:
        profile = kwargs.get("profile_name", "unknown")
        import datetime
        import json

        entry = {
            "event": "pre_tool_call",
            "profile_name": profile,
            "tool_name": kwargs.get("tool_name", ""),
            "timestamp": datetime.datetime.utcnow().isoformat(),
        }
        with open(_TOOL_EVENT_LOG, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass  # best-effort, never raise