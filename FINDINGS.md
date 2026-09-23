# Task t_7a8f126c — Find the second mechanism that rewrites card assignee to `architect`

## Goal
Cards silently get their assignee rewritten to `architect`; worker dies in ~23s with 0 tool calls, rc=0, logged as "protocol violation". Find the mechanism, reproduce it, fix it, add a regression test.

## ROOT CAUSE FOUND — THE MECHANISM

The **`hscc-escalate-watcher` Hermes cron job** reassigns failing kanban cards to the `architect` profile.

Pipeline (end to end):
1. Cron `6407ea32e1dd` `hscc-escalate-watcher`, `*/15 * * * *`, `enabled: true`, `repeat.completed: 5800`, `last_run_at` 2026-09-23T22:00, `last_status: ok` — registered in `~/.hermes/cron/jobs.json`.
2. Runs `scripts/escalate_watcher_run.py`, which imports `hscc_daemon.escalate_watcher` and calls `scan_and_escalate(fail_limit=3, strong_profile="architect")`.
3. `scan_and_escalate` (hscc_daemon/escalate_watcher.py) queries the live kanban DB:
   `SELECT ... FROM tasks WHERE status IN ('running','ready','blocked') AND consecutive_failures >= 1`.
4. For each row with `consecutive_failures >= 3` and `assignee != 'architect'`, `escalate.decide_escalation` returns `{"action":"escalate","reassign_to":"architect"}`.
5. `_default_reassign` runs `hermes kanban reassign <task_id> architect` → `reassign_task` → `assign_task` → `UPDATE tasks SET assignee='architect'` + `assigned` event.

So a card created via ANY path (incl. `hermes kanban create --assignee`) that accumulates 3 consecutive failures gets its assignee silently rewritten to `architect` — the hardcoded "strong tier".

## LIVE-DB EVIDENCE (hscc board + main board kanban.db)
- Dozens of `assigned: {"assignee":"architect"}` events clustered EXACTLY on `*/15` cron tick boundaries:
  - 2026-08-09 19:45 (4 cards), 23:15 (4 cards)
  - 2026-08-17 11:00, 2026-09-02 15:15/15:30, 2026-09-03 16:00
  - 2026-09-08 21:45/22:00, 2026-09-09 17:00, 2026-09-10 20:15, 2026-09-13 21:45, 2026-09-21 23:45
- t_b45e8e4a (hscc board): runs 265,275,277,279,282 profile=backend-engineer (crash then protocol violations), event [13578] `assigned: architect` at 2026-08-30 17:15:53 (right after run end 17:08:53, before architect run 283 at 17:15:55), then runs 283-285 profile=architect all crashed. cf=3.
- t_3fe0cd05: protocol violations on original assignee 16:43-16:50, then `assigned: architect` at 17:00 (escalate tick), more protocol violations as architect, then operator workaround `assigned: devops-engineer` at 06:49 + `unblocked`, then completed.
- Cards where current assignee != architect (e.g. t_299d0dbd→devops-engineer, t_3fe0cd05→devops-engineer, t_61f27161→hscc-orch) = the operator's manual `assign`+`unblock` workaround undoing the flip.

## Why the reassign is harmful
- `architect` cannot execute kanban worker cards here: its worker dies in ~1-5 min with rc=0, 0 tool calls → `protocol violation` (see t_b45e8e4a runs 283-285: exactly 5:00 each; t_3fe0cd05: ~1 min each).
- So escalating to `architect` never progresses the card — it only silently corrupts the assignee and burns dispatch cycles. Operator must manually `assign`+`unblock` it back, which "works every time" because the card was never the problem.

## Write-path audit (all kanban DB assignee writes)
- kanban_db.py `_canonical_assignee` — only lowercases/normalizes, never invents `architect`.
- `create_task` — writes caller's assignee (→ `architect` only if caller passed it).
- `assign_task` / `reassign_task` — write caller-supplied profile. Called by escalate watcher with `architect`.
- `request_review`(3075)/`request_changes`(3178)/`reopen_review_task`(3350)/`promote` — all preserve stored assignee / write the reviewer/implementer provenance; none default to architect.
- `specify_triage_task`(3490) — only when assignee arg given.
- `decompose_triage_task` (kanban_db_graph.py:151) — root rewritten to `root_assignee` = `orchestrator_profile` (falls back to ACTIVE PROFILE). Separate concern; not this bug (that's triage→todo roots, not ready cards). NOTE: if active default profile were `architect` this would ALSO rewrite — but default profile is `default`, not architect.
- DISPATCHER `_apply_default_assignee` — writes `kanban.default_assignee` (=`worker`) only to NULL-assignee rows. NOT architect.

## The FIX (implemented)
Acting escalation (reassign to strong tier) is now OPT-IN:
- `escalate.py: decide_escalation(..., strong_profile=None)` — only returns `escalate` when a strong profile is explicitly configured; default None routes at-threshold failures to `human`.
- `escalate_watcher.py: scan_and_escalate(..., strong_profile=None)` — same opt-in default.
- `scripts/escalate_watcher_run.py: STRONG_PROFILE = os.environ.get("HSCC_STRONG_PROFILE") or None` — was defaulting to `"architect"`; now None unless explicitly set.
- Human-reason string now names the failing assignee (was "strong tier (None)").

With no `HSCC_STRONG_PROFILE` env set (current operator state: not set), the cron now REPORTS stuck tasks to the human (via stdout → desktop delivery + `~/.hscc/escalated.json` dedup) instead of silently reassigning to `architect`.

## Regression tests (fail before fix, pass after)
- test_escalate_watcher.py::TestScanAndEscalate::test_default_does_not_reassign_to_architect — verified FAILS against original code (`assert 'escalate' == 'human'`), PASSES after.
- test_escalate.py::TestDecideEscalation::test_at_limit_non_strong — same.
- Opt-in acting path still covered: tests pass `strong_profile="architect"` explicitly.

## Verification needed
- Full suite green on 8 packages under BOTH interpreters.

## Corroborating in-repo doc
`docs/dispatch-assignee-validation.md` (from prior task t_d3c929aa) independently
documents the SAME pattern: cards created with correct assignee (devops-engineer/
backend-engineer) get "bulk reassigned to architect via assign_task(..., 'architect')
calls (plain assigned events, no source: default_assignee key)" on 08-14/08-30/09-02/
09-03/09-08, "events carry no actor/run_id, so the source is not yet attributable",
and recommends a follow-up to "gate whether bulk reassign to architect should be
allowed". My finding attributes that unattributed bulk reassign to the
hscc-escalate-watcher cron, and the fix implements the recommended gate (opt-in
strong profile).

## Constraints respected
- Did NOT change kanban caps (2/3/2), did NOT restart gateway, did NOT touch ~/.hermes/state.db. Did not re-litigate ruled-out paths.
- Redacted real infra addresses from all evidence (only placeholders 100.64.0.1/10.0.0.x used; gateway LAN address redacted).
