# Reviewer Loop (Phase 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Hermes' built-in review-dispatch path actually work end-to-end: a coder submits its finished task to `review` status, the dispatcher (already coded) spawns a review agent loading a new `sdlc-review` skill that verifies the work (diff + tests + spec), then merges the worktree to the integration branch (→ done) or rejects it back to the worker (→ running).

**Architecture:** Hermes core ALREADY contains the review-dispatch *consumer*: `dispatch_once` spawns a review agent for any task in `status='review'` (kanban_db.py:6273-6330), force-loading the `sdlc-review` skill; `claim_review_task` transitions review→running; `has_spawnable_review` gates concurrency. What's missing is (1) a *producer* — nothing moves a task INTO `review` status — and (2) the `sdlc-review` skill itself (referenced but absent on disk). This phase supplies both, using the existing `kanban_edit` tool (which already accepts any status in VALID_STATUSES) as the status-transition mechanism — no new core transitions needed. We also REVERT Phase 2's separate-review-task creation, because in the built-in model the coder self-submits its own task to review rather than spawning a sibling review task.

**Tech Stack:** Python 3 (hermes-agent core, `local-custom` branch — confirm with `git -C /Users/desac/.hermes/hermes-agent branch --show-current`), a new skill authored as a Hermes skill (`~/.hermes/skills/sdlc-review/SKILL.md` + bundled into `hscc-skills`), pytest.

**Branch:** Core/prompt changes on `hermes-agent` `local-custom` (never pushed upstream). The skill + hscc-skills bundling go in `pom11/hscc` (pushed).

**Scope:** Phase 3 ONLY — the working reviewer loop on the built-in `review` path. Per user decision: USE THE BUILT-IN MECHANISM AS-IS, drop our custom retry-counter/separate-task design. No custom rejection counter (the built-in reject→running re-runs the worker; bounding is left to the existing spawn-failure breaker + the reviewer's judgment in the skill). The autonomy governor + phrase trigger + spawn base_url injection are Phase 4.

**Out of scope (Phase 4+):** autonomy on/off switch, "do it autonomously" phrase trigger, auto-role-creation, spawn base_url injection, per-node model routing.

---

## Pre-flight: revert Phase 2's separate-review-task creation

Phase 2 made the decomposer append separate `reviewer`-assigned tasks. The built-in path instead has the coder move ITS OWN task to `review`. These conflict (you'd get both a self-review AND a sibling review task). Phase 3 Task 1 disables the Phase 2 auto-pairing. We keep the `_pair_review_tasks`/`_review_policy` code (harmless, tested) but stop calling it, and remove the `kanban.auto_review` config so it's inert. Rationale: the built-in path is the chosen mechanism; the Phase 2 transform becomes dead-but-retained (could be deleted in cleanup later, but leaving it avoids a noisy revert and keeps its tests green).

---

## Task 1: Disable Phase 2 auto-pairing (reconcile with built-in path)

**Files:**
- Modify: `hermes-agent/hermes_cli/kanban_decompose.py` (the call site added in Phase 2)
- Modify: `~/.hermes/config.yaml` (remove/empty `kanban.auto_review`)
- Test: `hermes-agent/tests/test_kanban_review_pairing.py`

- [ ] **Step 1: Write the failing test**

Append to `hermes-agent/tests/test_kanban_review_pairing.py`:
```python
def test_decompose_no_longer_autopairs_reviews(monkeypatch):
    """Phase 3: the built-in review path replaces Phase 2 auto-pairing.
    With auto_review unset (the new default), decompose must NOT append
    reviewer tasks — the coder self-submits to review instead."""
    from hermes_cli import kanban_decompose as kd
    # Even if a stray policy somehow loaded, the call site is removed, so the
    # children passed to the DB equal the LLM children. We assert the seam:
    # _review_policy on the SHIPPED config returns {} (auto_review removed).
    from hermes_cli.config import load_config
    assert kd._review_policy(load_config()) == {}
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /Users/desac/.hermes/hermes-agent && venv/bin/python -m pytest tests/test_kanban_review_pairing.py::test_decompose_no_longer_autopairs_reviews -v`
Expected: FAIL (config still has `kanban.auto_review` from Phase 2 → `_review_policy` returns a non-empty dict)

- [ ] **Step 3: Remove the config + the call site**

(a) In `~/.hermes/config.yaml`, delete the entire `auto_review:` block under `kanban:` (the `reviewer:` + `review_roles:` lines added in Phase 2). Back up first: `cp ~/.hermes/config.yaml ~/.hermes/config.yaml.bak-$(date +%Y%m%d-%H%M%S)`.

(b) In `hermes-agent/hermes_cli/kanban_decompose.py`, find the Phase-2 wiring line inside `decompose_task`:
```python
    # Auto-pair a reviewer task onto each code-producing child (Phase 2).
    # Policy-driven: kanban.auto_review.{review_roles, reviewer}. No-op when
    # unconfigured. Review children are appended after all impl children so the
    # impl parent indices above stay valid; each review is gated behind its impl.
    children = _pair_review_tasks(children, _review_policy(cfg))
```
Replace it with a one-line note (keep the functions defined, just stop calling them):
```python
    # NOTE: review is handled by the built-in review-status path (coders submit
    # their own task to 'review'; the dispatcher spawns an sdlc-review agent).
    # The Phase-2 auto-pairing (_pair_review_tasks) is intentionally NOT called.
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd /Users/desac/.hermes/hermes-agent && venv/bin/python -m pytest tests/test_kanban_review_pairing.py -v`
Expected: all pass (the helper unit tests still pass — functions still exist; the new test passes because auto_review is gone from config). Also confirm decompose still imports: `venv/bin/python -c "from hermes_cli import kanban_decompose"`.

- [ ] **Step 5: Commit**

```bash
cd /Users/desac/.hermes/hermes-agent
git branch --show-current   # local-custom
git add hermes_cli/kanban_decompose.py tests/test_kanban_review_pairing.py
git commit -m "refactor(kanban): use built-in review path; stop Phase-2 auto-pairing"
```

---

## Task 2: Worker submit-to-review — new transition + tool + guidance

**Files:**
- Modify: `hermes-agent/hermes_cli/kanban_db.py` (add `submit_review_task` transition near `block_task` ~line 4047)
- Modify: `hermes-agent/tools/kanban_tools.py` (add `kanban_submit_review` worker tool: schema + handler + registration, mirroring `kanban_block`)
- Modify: `hermes-agent/agent/prompt_builder.py` (KANBAN_GUIDANCE, line 181; review-required text at line 222)
- Test: `hermes-agent/tests/test_kanban_submit_review.py`

**Context (verified):** There is NO `kanban_edit` worker tool. Worker-callable kanban tools are: show, list, complete, block, heartbeat, comment, create, unblock, link. None sets `review` status. So we MUST add a producer: a transition `submit_review_task(conn, task_id)` (running→review) + a `kanban_submit_review` tool. The dispatcher consumer (`status='review'` → spawn sdlc-review agent) already exists. `_handle_block` (kanban_tools.py:633) is the exact pattern to mirror (ownership check via `_enforce_worker_task_ownership`, `_default_task_id`, `_connect`).

- [ ] **Step 1: Write the failing test**

`hermes-agent/tests/test_kanban_submit_review.py`:
```python
import os
import tempfile
from hermes_cli import kanban_db as kb


def _fresh_board(tmp):
    os.environ["HERMES_KANBAN_DB"] = os.path.join(tmp, "kanban.db")
    conn = kb.connect()
    kb.init_db(conn)
    return conn


def test_submit_review_moves_running_to_review(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "k.db"))
    conn = kb.connect()
    kb.init_db(conn)
    tid = kb.create_task(conn, title="build", assignee="coder",
                         initial_status="running")
    ok = kb.submit_review_task(conn, tid)
    assert ok is True
    task = kb.get_task(conn, tid)
    assert task.status == "review"


def test_submit_review_rejects_non_running(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "k.db"))
    conn = kb.connect()
    kb.init_db(conn)
    tid = kb.create_task(conn, title="x", assignee="coder",
                         initial_status="blocked")
    ok = kb.submit_review_task(conn, tid)
    assert ok is False  # only running tasks may be submitted to review
```
NOTE: adapt `create_task`/`connect`/`init_db` calls to the real signatures in kanban_db.py (read them first — `create_task` takes keyword args; `initial_status` must be in VALID_INITIAL_STATUSES = {running, blocked}). The ASSERTION is the contract: running→review returns True + sets status; non-running returns False.

- [ ] **Step 2: Run to verify it fails**

Run: `cd /Users/desac/.hermes/hermes-agent && venv/bin/python -m pytest tests/test_kanban_submit_review.py -v`
Expected: FAIL with `AttributeError: module 'hermes_cli.kanban_db' has no attribute 'submit_review_task'`

- [ ] **Step 3a: Add the transition to kanban_db.py**

Near `block_task` (~line 4047), add (read `block_task` first to match the exact `write_txn` + `_append_event` style used in this file):
```python
def submit_review_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """Transition ``running -> review`` so the dispatcher spawns a review agent.

    Returns True on success, False if the task is not currently running. The
    review consumer (claim_review_task + the review-column dispatch) is already
    implemented; this is the producer side. The task keeps its worktree, so the
    review agent inherits the same workspace.
    """
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET status = 'review', claim_lock = NULL, "
            "claim_expires = NULL WHERE id = ? AND status = 'running'",
            (task_id,),
        )
        if cur.rowcount != 1:
            return False
        _append_event(conn, task_id, "submitted_review", {})
        return True
```
IMPORTANT: clearing `claim_lock`/`claim_expires` is required so the review-column dispatch (which selects `WHERE status='review' AND claim_lock IS NULL`) can pick it up. Verify `_append_event` signature + that an event-type string is free-form (match how `block_task` emits events).

- [ ] **Step 3b: Add the worker tool to kanban_tools.py**

Mirror `_handle_block` + its schema + registration. Add a schema constant `KANBAN_SUBMIT_REVIEW_SCHEMA` (task_id optional + board optional, like block but no reason), a handler:
```python
def _handle_submit_review(args: dict, **kw) -> str:
    """Submit the worker's own running task to review (running -> review)."""
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error("task_id is required (or set HERMES_KANBAN_TASK)")
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            ok = kb.submit_review_task(conn, tid)
        finally:
            conn.close()
    except Exception as e:
        return tool_error(f"kanban_submit_review: {e}")
    if not ok:
        return tool_error(
            "submit_review failed — task must be in 'running' status (yours)")
    return _ok(task_id=str(tid), status="review")
```
and register it (mirror the block registration block, `check_fn=_check_kanban_mode`, emoji e.g. "🔎"). Match the EXACT helper names/return shapes used by the other handlers in this file (`_ok`, `tool_error`, `_connect`, `_default_task_id`, `_enforce_worker_task_ownership`) — read a couple of existing handlers to confirm signatures (some use `with _connect(...)` context, some `conn.close()` in finally; follow whichever the file actually uses).

- [ ] **Step 3c: Update KANBAN_GUIDANCE (prompt_builder.py)**

Replace the line-222 instruction `kanban_block(reason="review-required: ...")` so code tasks route to the new tool:
```
    "Exception: if your output is a code change that needs review before it "
    "counts as done (most coding tasks), put the structured metadata "
    "(changed_files / tests_run / diff summary) in a kanban_comment, then call "
    "kanban_submit_review instead of kanban_complete. This hands your task to "
    "an automated reviewer that checks the diff, runs the tests, and either "
    "merges your work or sends it back to you with change requests. Do NOT "
    "kanban_complete a code task yourself.\n"
```

- [ ] **Step 4: Run tests + checks**

```bash
cd /Users/desac/.hermes/hermes-agent
venv/bin/python -m pytest tests/test_kanban_submit_review.py -v   # 2 passed
venv/bin/python -c "from agent.prompt_builder import KANBAN_GUIDANCE; assert 'kanban_submit_review' in KANBAN_GUIDANCE and 'review-required' not in KANBAN_GUIDANCE; print('guidance OK')"
venv/bin/python -c "from tools import kanban_tools; from tools.registry import registry; assert 'kanban_submit_review' in registry.get_all_tool_names() or 'kanban_submit_review' in getattr(registry,'_tools',{}); print('tool registered')"
venv/bin/python -m pytest tests/ -k "kanban" -q   # no regressions in isolated kanban tests
```

- [ ] **Step 5: Commit**

```bash
cd /Users/desac/.hermes/hermes-agent
git add hermes_cli/kanban_db.py tools/kanban_tools.py agent/prompt_builder.py tests/test_kanban_submit_review.py
git commit -m "feat(kanban): kanban_submit_review tool + running->review transition"
```

---

## Task 3: Author the `sdlc-review` skill

**Files:**
- Create: `plugins/hscc-roles/../` → actually create at `~/.hermes/skills/sdlc-review/SKILL.md` AND the repo-tracked copy. Decision: bundle it via `hscc-skills`. Create the source at `plugins/install/hscc-skills/sdlc-review/SKILL.md` (the installer source), then add `sdlc-review` to `BUNDLED_SKILLS` in `plugins/hscc-skills/hscc.py`, then install it to `~/.hermes/skills/`.

**Context:** The dispatcher force-loads `sdlc-review` (kanban_db.py:6324 `claimed.skills = ["sdlc-review"]`) for review agents. The skill is the reviewer's brain. It must: read the diff in the task's worktree, run the tests, check the work matches the task spec, then EITHER merge to the integration branch + `kanban_complete` (→ done) OR write change-request comments + `kanban_edit(status="ready")` (back to the worker — the built-in reject). The review agent runs in the SAME worktree as the original task (claim_review_task keeps the workspace).

- [ ] **Step 1: Write the skill source**

Create `plugins/install/hscc-skills/sdlc-review/SKILL.md`:
```markdown
---
name: sdlc-review
description: "Autonomous code review for a kanban task in review status. Verifies diff + tests + spec, then merges to the integration branch or sends the task back to the worker with change requests."
---

# SDLC Review

You are reviewing a kanban task that a worker submitted to `review` status. You
are running in that task's git worktree. Your job is a strict, honest quality
gate — approve only work that is correct, tested, and matches the spec.

## The review bar (ALL THREE required to approve)

1. **Diff is sound.** Read the actual diff (`git -C <worktree> diff <base>...HEAD`
   or `git log -p`). Look for correctness bugs, missed edge cases, silent
   failures, and anything that does not belong.
2. **Tests are green.** Run the task's tests. If the task body names a test
   command, run exactly that. Otherwise run the project's test suite for the
   changed area. Do not trust the worker's claim — run them yourself.
3. **Spec is met.** Re-read the task's title + body (`kanban_show <task_id>`).
   Confirm the work actually does what the task asked. Well-written code that
   solved the wrong thing is a REJECT.

## On APPROVE (all three pass)

1. Merge the worktree branch into the integration branch:
   - Determine the integration branch (default: `integration`; create it from
     the project's default branch if it does not exist).
   - `git -C <project-dir> merge --no-ff <task-branch> -m "merge: <task title>"`
     into `integration` (NOT main — main stays human-gated).
   - If the merge conflicts, do NOT force it — REJECT with the conflict details
     so the worker rebases.
2. `kanban_complete(summary="approved + merged to integration: <one line>")`.

## On REJECT (any of the three fails)

1. Write a precise, actionable change request via `kanban_comment` — exact
   files/lines, what's wrong, what "done" looks like. No vague feedback.
2. `kanban_edit(status="ready")` to send the task back to its original worker.
   (The dispatcher will re-spawn the worker, which reads your comment and fixes.)
3. Do NOT merge anything.

## Rules

- Never merge to `main` — only to the integration branch.
- Never approve without running the tests yourself.
- Be specific in rejections; the worker acts only on what you write.
- If you cannot determine the test command or the spec is ambiguous, REJECT
  with a request for clarification rather than guessing.
```

- [ ] **Step 2: Bundle it via hscc-skills**

In `plugins/hscc-skills/hscc.py`, add `"sdlc-review"` to the `BUNDLED_SKILLS` list (in the HSCC cluster-control skills section).

Run: `cd /Users/desac/.hermes/plugins/hscc-skills && ../../hermes-agent/venv/bin/python -c "import hscc; assert 'sdlc-review' in hscc.BUNDLED_SKILLS; print('bundled OK')"`

- [ ] **Step 3: Install the skill to ~/.hermes/skills**

Run: `cd /Users/desac/.hermes/plugins/hscc-skills && ../../hermes-agent/venv/bin/python hscc.py install-skills 2>&1 | tail -5`
Then verify: `ls ~/.hermes/skills/sdlc-review/SKILL.md` exists.

- [ ] **Step 4: Verify Hermes can resolve the skill**

Run: `cd /Users/desac/.hermes && hermes-agent/venv/bin/python -m hermes_cli.main skills list 2>&1 | grep -i sdlc-review`
Expected: shows `sdlc-review`.

- [ ] **Step 5: Commit**

```bash
cd /Users/desac/.hermes/plugins
git add hscc-skills/hscc.py install/hscc-skills/sdlc-review/
git commit -m "feat(hscc-skills): add sdlc-review skill for the built-in review loop"
git push origin main
```

---

## Task 4: End-to-end verification on a throwaway board

**Files:** none (operational verification).

**Note:** This requires the gateway running to actually spawn agents. Per the user's standing instruction NOT to start the gateway without explicit go, this task is a STRUCTURED DRY VERIFICATION of the wiring + a documented manual e2e the user can run later. Do NOT start the gateway as part of this plan.

- [ ] **Step 1: Static wiring check — review dispatch reads our skill**

Run: `cd /Users/desac/.hermes/hermes-agent && grep -n 'claimed.skills = \["sdlc-review"\]' hermes_cli/kanban_db.py`
Expected: the line exists (confirms dispatcher force-loads our now-existing skill).

- [ ] **Step 2: Static wiring check — the review-status producer exists**

Run: `cd /Users/desac/.hermes/hermes-agent && venv/bin/python -c "
from agent.prompt_builder import KANBAN_GUIDANCE
assert 'kanban_submit_review' in KANBAN_GUIDANCE
print('producer (worker submits to review) OK')
"`
Expected: OK.

- [ ] **Step 3: Confirm the producer transition + tool exist**

Run: `cd /Users/desac/.hermes/hermes-agent && venv/bin/python -c "
from hermes_cli.kanban_db import VALID_STATUSES, submit_review_task
from tools import kanban_tools  # registers the tool
assert 'review' in VALID_STATUSES
assert callable(submit_review_task)
print('submit_review_task transition + review status OK')
"`

- [ ] **Step 4: Document the manual e2e for the user**

Write a short note (in the commit message / session) describing the manual end-to-end the user runs after the next gateway start:
1. `hermes kanban create "trivial code task with a test"` assigned to `coder`.
2. Watch: coder builds in worktree → `kanban_edit(status=review)`.
3. Watch: dispatcher spawns sdlc-review agent → runs tests → merges to `integration` (or rejects to `ready`).
4. `git -C <project> branch` shows `integration` with the merge; `main` untouched.

- [ ] **Step 5: Final commit (docs/note only)**

```bash
cd /Users/desac/.hermes/plugins
git add docs/superpowers/plans/2026-06-09-reviewer-loop-phase3.md
git commit -m "docs: Phase 3 reviewer-loop plan + manual e2e steps"
git push origin main
```

---

## Self-Review

**Spec coverage (Phase 3 / design's reviewer loop):**
- Autonomous reviewer acts on submitted work → built-in dispatch + Task 3 skill ✓
- Strict gate (diff + tests + spec) → Task 3 skill review bar ✓
- Approve → merge to integration branch (main human-gated) → Task 3 APPROVE section ✓
- Reject → back to worker → Task 3 REJECT (`kanban_edit(status="ready")`) + built-in re-spawn ✓
- Worker submits code for review → Task 2 (KANBAN_GUIDANCE) ✓
- Reconcile/replace Phase 2 separate-task design → Task 1 ✓

Per user decision, NO custom retry counter — the built-in reject→ready→re-spawn loop plus the existing spawn-failure breaker bound it. Deferred to Phase 4: autonomy switch, phrase trigger, spawn injection.

**Placeholder scan:** none — skill content + code edits are concrete. Task 4 is explicitly a dry/structured verification because the gateway must stay stopped per user instruction; the manual e2e is documented for the user to run.

**Type/consistency:** uses existing `kanban_edit` (status arg, validated against VALID_STATUSES incl. `review`), existing `claim_review_task`/dispatch consumer, existing `kanban_complete`/`kanban_comment`. No new core transition invented. The skill name `sdlc-review` matches the dispatcher's hardcoded `claimed.skills = ["sdlc-review"]` exactly. Integration-branch merge target consistent with the design (main stays human-gated).

**Risk:** Task 2 (prompt) + Task 1 (decompose) are local-custom core edits — both small and reversible. The skill is additive. The review loop only activates for tasks that reach `review` status, which only happens once Task 2's guidance is live AND a coder runs — so nothing changes behaviour until the gateway is next started with a real coding task. Safe to stage.
