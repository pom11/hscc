# WS4 — Agentic Work-Flows: resume / review / docs — Design + Spike

**Spec:** §WS4, D3/D11, G6. **Status:** design (the prize — built on hermes-native kanban).
**Depends:** the kanban fork (`feat/kanban-submit-review`, present).

## Why a design doc before code
The resume completion-probe (G6) was flagged in the master spec as the
highest-uncertainty item — "don't hand-wave it." This doc resolves the open
questions against the REAL kanban internals (explored, not assumed), defines the
probe precisely, and leaves exactly one decision for the user before build.

## What already exists (don't rebuild)
Explored in `~/.hermes/hermes-agent` (the fork):
- **States** (`kanban_db.py:100`): `triage→todo→…→running→review→done` + `blocked/archived`.
- **Review flow** (`tools/kanban_tools.py:671` `_handle_submit_review`): a worker
  calls `kanban_submit_review` → `running→review`; the dispatcher spawns a review
  agent. `kanban_decompose._pair_review_tasks` (policy `kanban.auto_review`)
  auto-creates paired review tasks during decompose.
- **Run history** (`task_runs` table): every claim = a run row with
  `status/outcome/summary/error/metadata`. `tasks.consecutive_failures` +
  `current_run_id` + `result` + `branch_name` already tracked.
- **Worker context** (`kanban_db.py:6947` `build_worker_context`): a re-dispatched
  worker ALREADY sees its prior attempts (`_CTX_MAX_PRIOR_ATTEMPTS=10`), parent
  handoffs, role history, comments. Reclaim of stale claims is built in.
- **Circuit breaker**: `_record_task_failure` trips at `failure_limit`.

**Conclusion:** review (D3/D11) is largely a config + SOUL/role wiring job on top
of what the fork provides. The genuinely-new piece is the **resume probe** — and
it must augment, not duplicate, `build_worker_context`.

## The three pieces

### 4a — Idempotent resume (G6, the spike)
**Problem.** kanban tracks RUN history (attempts) but not WORK-PRODUCT state. A
re-dispatched worker is told "you tried before" but not "here is what already
landed on your branch." So it can redo finished work.

**Probe definition (resolved).** The unit of progress is the **task**, and
"satisfied" is judged from three signals, in order:
1. **Kanban truth first.** If the task is already `done`/`review`, do not
   re-dispatch (kanban is authoritative for lifecycle).
2. **Git work-product.** On the task's `branch_name`: does the branch exist, and
   does `git diff <base>...<branch>` touch the files the plan's checklist names?
   A non-empty, on-target diff = partial-or-complete work present.
3. **Tests.** If the task/plan names tests, run them on the branch. Green +
   on-target diff = **satisfied** → move to review, don't redo. Red or no diff =
   **resume** from the first unsatisfied checklist item.

"Done vs abandoned mid-edit" is disambiguated by (1) kanban status and (2)
whether the last run's `outcome` was `completed` vs `crashed/timed_out/reclaimed`
— both already in `task_runs`. An abandoned mid-edit (crashed outcome, dirty
branch) → resume; a clean completed run → already done.

**Delivery.** A pure helper `hscc-roles` (or a small `hscc-workflow` module) —
`probe_task_state(task, branch, plan) -> {satisfied, resume_from, evidence}` —
and a hook that prepends its result to `build_worker_context` via a kanban
comment or the worker preamble (NOT a core patch — use the comment thread the
context builder already surfaces). The probe shells `git` read-only.

**Spike scope (do FIRST, before wiring):** prototype `probe_task_state` against a
throwaway git repo + a fake task dict; prove the three signals + the
completed-vs-crashed disambiguation on real `git diff`/`git branch` output. Only
after the spike passes do we wire it into dispatch.

### 4b — Review flow (D3/D11)
Build on the fork's `kanban_submit_review` + `auto_review` pairing. HSCC's job:
- Enable `kanban.auto_review` in config (policy: pair a reviewer task per coder
  task) — a bootstrap/config wiring step.
- A **reviewer role** (via `hscc-roles`) whose SOUL encodes the strict bar from
  the fleet spec: approve only if (1) diff read for correctness, (2) task tests
  run green, (3) work matches the plan. Else reject → comment with the gap.
- Tiered retry: the circuit breaker already trips at `failure_limit`; set it so
  N rejects → escalate to the user (a kanban comment + notify). Approved work
  lands on the integration branch; main stays human-gated (D11).

### 4c — Doc-driven execution
The pattern this whole effort used IS the deliverable: design doc → per-WS plan →
tasks → build → review. Encode it as orchestrator guidance (already partly in the
WS1 SOUL block): "for non-trivial work, write a spec+plan into
`docs/superpowers/`, decompose into kanban tasks, the plan checklist is the
acceptance contract the reviewer checks against." Add a `hscc-roles` architect
disposition that produces the plan, and make the reviewer check work-vs-plan.

## Decisions (locked with user)
- **Reject→escalate threshold = 3.** Same coder retries up to 3×; the 3rd reject
  escalates to the user with full review history (kanban comment + notify). Wire
  via the existing circuit breaker `failure_limit`.
- **Merge target = `integration` branch; `main` human-gated.** Approved work
  auto-merges to `integration`; the user promotes `integration→main`.
- **Build the probe as a SPIKE first** (throwaway git repo) before any wiring.

## Build order (after the decision)
1. **Spike** `probe_task_state` (throwaway repo, prove the 3 signals). Commit only
   if the spike is convincing; else revise the probe definition.
2. 4a resume helper + dispatch hook (comment-based, no core patch) + tests.
3. 4b reviewer role + `auto_review` config + escalation wiring + tests.
4. 4c orchestrator/architect guidance + the doc-driven loop.

## Testing
- Spike + unit: `probe_task_state` satisfied/partial/abandoned/done against real
  git fixtures (the mock-vs-real rule — assert on real `git` output, not stubs).
- Review: pairing creates a reviewer task; reject increments + escalates at N;
  approve → integration branch.
- No live dispatch in tests; a dry-run pipeline test drives the state machine.

## Out of scope (now)
- Patching kanban core (use comments + config + roles; keep the fork delta小).
- Auto-merge to main (always human, D11).
