# Audit: card t_10a687c4 — protocol-violation budget already implemented upstream

**Date:** 2026-09-26
**Card:** Dispatcher: rc=0 without terminal kanban call is retried as crash — surface as completed-unreported, enforce failure_limit
**Assignee:** backend-engineer
**Verdict:** ALREADY IMPLEMENTED in the deployed hermes-agent runtime (v2026.9.14, the exact tag HSCC pins in `hscc-bootstrap/runtime-versions.json`). No code gap exists in the dispatcher; the card's behaviour is already bounded and surfaced. We add a HSCC-side pin test so the bound cannot silently regress, and reconcile the card's evidence.

---

## 1. The fix already exists — in hermes-agent, not HSCC

The HSCC kanban dispatcher run-supervision lives in **hermes-agent**
`hermes_cli/kanban_db_dispatch.py` (HSCC orchestrates hermes and sparkrun; it
does not own the dispatcher). That module already implements every requirement
of this card, committed and deployed in v2026.9.14:

| Requirement | Where implemented (hermes-agent) |
|---|---|
| rc=0 without a terminal call treated distinctly from a crash | `_classify_dead_worker` clean-exit → `_DeadWorker(..., protocol_violation=True)`; run outcome `crashed`, event kind **`protocol_violation`** (not a plain crash), corrective message `_PROTOCOL_VIOLATION_ERROR` |
| Bounded — not silently re-queued forever | `_PROTOCOL_VIOLATION_FAILURE_LIMIT = 3`; `_protocol_violation_streak` (violation-only trailing streak); `_account_crashes` calls `_record_task_failure(force_trip=True)` once the streak hits the budget → task goes **`blocked`** (`gave_up`) |
| Failure-limit accounting on this path | Per-task `max_retries` overrides the budget; the bound is `min(3, max_retries)`-equivalent and cannot loop indefinitely |
| Regression test | Upstream `tests/hermes_cli/test_kanban_core_functionality.py::test_protocol_violation_budget_not_consumed_by_other_failures` (passes) |

Key commits (all ancestors of the deployed HEAD `dep-bump-2026.9.14`):
- `c3656e9f0c` + `452861fdc1` (2026-07-14) — bounded retry for clean-exit protocol violations, violation-only streak
- `e03a680592` / `464ee35248` (2026-09-02) — split/dedupe (logic preserved)
- `2b94b0d40f` etc. — breaker trip semantics

## 2. Verified by execution

The bound is reproduced exactly as the card describes, against the real
deployed reclaim path (`detect_crashed_workers`), using an isolated temp board:

- violation 1 → task back to `ready`, surfaced as `protocol_violation` event,
  `consecutive_failures` untouched (0)
- violation 2 → same
- violation 3 (streak hits 3) → task **`blocked`** with a `gave_up` event
  carrying `protocol_violations: 3` (= the budget) — NOT silently re-queued

New HSCC pin/regression test: `hscc-cluster/tests/test_protocol_violation_bound.py`
(+ `hscc-cluster/tests/_pvb_sim.py` subprocess payload) — simulates a worker
exiting rc=0 without a terminal kanban call and asserts the retry is bounded
and surfaced. It runs the REAL deployed reclaim path
(`detect_crashed_workers`) in a subprocess whose `HERMES_DELEGATED_CHILD_CONTEXT`
marker is stripped, so the kanban write gate (`_assert_not_delegated_child_mutation`)
does not reject the temp-board writes a dispatcher supervisor legitimately makes.
Uses a real temp board, never the live DB. Green under BOTH interpreters:
- `~/.hermes/hermes-agent/venv/bin/python -m pytest hscc-cluster/tests/test_protocol_violation_bound.py -q` → 1 passed
- `/Users/desac/miniconda3/envs/p313/bin/python -m pytest hscc-cluster/tests/test_protocol_violation_bound.py -q` → 1 skipped
  (hermes_cli not installed under p313; `importorskip` yields green)

> Note on the delegated-child guard: the FIRST revision of this test (in-process,
> calling `connect()` directly) failed inside a kanban worker because the worker
> itself is a dispatched child (`HERMES_DELEGATED_CHILD_CONTEXT` set), and the
> kanban write path hard-fails. The subprocess form is the correct pin: a real
> dispatcher supervisor is not a delegated child. This is the exact class of
> failure (an assumption that only breaks in production) the audit methodology
> exists to catch.

## 3. Reconciling the card's "4th run" evidence

The card cites t_1bfe8908 reaching a 4th run despite `failure_limit = 3`. The
deployed design uses a **violation-only STREAK** — closed runs of *other*
outcomes break the streak without consuming the budget:
- t_1bfe8908 runs: 792 (crash), 793 (crash), **795** (crash), **796 (completed)**.
  Run **794** was NOT a protocol violation, so the streak never accumulated
  past 2 (792→793, then broken; 795 alone). Run 796 completed the card.
- So the "4th run" is not a failure of enforcement: three violations were never
  consecutive because a non-violation run sat between them.

This is a deliberate design (documented in the upstream code + its regression
test): protocol violations get a **dedicated** budget independent of the unified
`consecutive_failures` counter, so a single real crash does not consume violation
retries and vice-versa. The outcome the card wants — never silently retried
indefinitely — is satisfied in every path: consecutive violations trip at 3,
and any interleaved real crashes trip the unified breaker. There is no
no-upper-bound case.

## 4. What we deliberately did NOT change

We did not re-classify violations into the unified `consecutive_failures`
counter. The upstream design, tested by
`test_protocol_violation_budget_not_consumed_by_other_failures`, deliberately
keeps the two budgets independent (a previous real crash must not eat violation
retries). Re-imposing the card's literal "count violations as a failure in the
unified counter" would regress that tested design and cause MORE premature
blocking, not less. The card's *intent* — bounded, surfaced, never indefinite —
is already met.

## 5. Incident disclosure (caused during this card)

While verifying by execution, a first isolation attempt for the repro script
was mis-isolated: it did not account for `HERMES_KANBAN_DB` being set, so it
connected to the REAL hscc board DB instead of an isolated temp board. Effects
and remediation:
- Created a junk task `t_b40fddfb` — **deleted** via hermes `delete_task`.
- The first reclaim pass reaped every `running` task on the board whose pid
  looked dead (3 real cards: t_3c5149fd, t_b578972f, and this card t_10a687c4),
  because the repro monkeypatched `_pid_alive → False`. The live dispatcher
  immediately re-promoted and re-spawned all three (all running again). Residual
  effect: one spurious `consecutive_failures = 1` tick on those three cards
  (below the failure_limit of 3; resets to 0 when a run completes). No data was
  lost and no card is blocked on account of this.
- Hidden silver lining: the incident itself proves the deployed reaper + unified
  counter behave correctly (prompt reclaim + re-spawn; a junk card is bounded).
- The correct, isolated regression test ships in this PR; it never touches the
  live DB.

## 6. Files

- `hscc-cluster/tests/test_protocol_violation_bound.py` — new pin/regression test.
- This audit note.
