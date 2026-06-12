"""Idempotent resume probe for the agentic work-flow (WS4 / G6).

kanban tracks RUN history (attempts) but not WORK-PRODUCT state. Before a
re-dispatched worker redoes a task, `probe_task_state` judges "is this already
satisfied?" from three signals, kanban-truth first:

  1. lifecycle: task already done/review  → satisfied (don't re-dispatch)
  2. git work-product: the task branch exists AND its diff vs base touches the
     plan's target files  → work is present
  3. tests: if the plan names tests, run them on the branch; green + on-target
     diff → satisfied; else resume from the first unsatisfied checklist item

"done vs abandoned mid-edit" is disambiguated by the last run outcome
(completed vs crashed/timed_out/reclaimed) + branch dirtiness.

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


def on_pre_kanban_dispatch(task_id=None, run_id=None, task=None, conn=None,
                           repo=None, **kwargs):
    """Hook handler for `pre_kanban_dispatch` (fires on re-dispatch, run_id>1).

    Posts a resume note (from the task branch's committed state) as a kanban
    comment, which build_worker_context surfaces to the re-dispatched worker so
    it resumes instead of redoing. Best-effort: never raises (the core fires
    this in a try/except, but we double-guard).

    ``conn`` is the live board connection passed by claim_task — used so the
    comment lands on the SAME board the task lives on. ``repo`` is the task's
    worktree; defaults to the task workspace_path, then cwd."""
    try:
        import os as _os
        t = task if isinstance(task, dict) else {
            "branch_name": getattr(task, "branch_name", None),
            "workspace_path": getattr(task, "workspace_path", None),
        }
        work_repo = repo or t.get("workspace_path") or _os.getcwd()
        note = resume_note(t, repo=work_repo)
        if not note or not task_id:
            return None
        from hermes_cli import kanban_db as _kb
        if conn is not None:
            _kb.add_comment(conn, task_id, author="hscc-resume", body=note)
        else:
            board = kwargs.get("board")
            c = _kb.connect(board=board) if board else _kb.connect()
            try:
                _kb.add_comment(c, task_id, author="hscc-resume", body=note)
            finally:
                c.close()
        return {"posted": True, "task_id": task_id}
    except Exception:
        return None
