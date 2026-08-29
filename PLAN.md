# PLAN — t_9a5cfc3b: approvals inbox

## What the feature is

When a kanban worker hits something destructive (a forced push, a tear-down, a
credential edit) it stops and blocks, waiting for a human to decide. Today that
decision is invisible from the phone: the operator sees "Board Hygiene → Blocked
→ Recover", a generic board-hygiene surface, buried inside the Cluster tab.

This card builds a purpose-built **Approvals inbox** — the operator's phone shows
"worker wants to force-push X — allow?" as a prominent, decision-focused list,
and the operator approves (or declines by leaving it blocked) from their phone.
This is the most phone-shaped feature in the system.

## Definition of a "pending approval"

A card is a pending approval (a human decision is required) iff it is **blocked**
with a decision-waiting `block_kind`. The `kanban_block` tool's kinds are:

  * `needs_input`  — waiting on a human answer.  → APPROVAL (human decision)
  * `capability`   — a hard wall: missing credential / a no-agent-can-do action. → APPROVAL
  * `dependency`   — waiting on another task; auto-resumes. → NOT an approval
  * `transient`    — flaky failure, auto re-queued.          → NOT an approval
  * (missing)      — dispatcher circuit-breaker auto-block with no kind. → APPROVAL
                    (operator must judge whether to allow re-run, hence it shows)

So: approvals = blocked cards whose kind is NOT `dependency`/`transient`.

## Source of truth (the API already has it — no new endpoint needed)

  * READ:  `GET /v1/kanban/blocked` → `KanbanBlockedResponse` / `BlockedCard`
           (fields: board, id, status, assignee, age_days, block_kind, why,
           title, comments). Backed by `hscc_daemon.kanban_blocked`, tested in
           hscc-api/tests/test_routes_kanban.py.
  * ALLOW: `POST /v1/kanban/blocked/{id}/recover` (confirm-gated, one card).
           Backed by `recover_blocked_task` — the honest "let it re-run" path.
  * DENY:  There is NO backend deny mutation, and we do NOT invent one. The
           honest decision surface is Allow (recover) or leave blocked. A
           fabricated deny endpoint would diverge from the daemon and add
           dangerous surface. State this clearly rather than pretending.

## iOS scope (primary deliverable)

1. **`ApprovalsView.swift`** (new) — a decision inbox:
   - Lists pending approvals (blocked cards of non-dependency/transient kind),
     newest-weighted, each row showing: title ("worker wants to X"), the `why` /
     `block_kind`, the `comments` (the actual request/context), assignee, board,
     age.
   - **Allow** action per row → confirm-gated `recoverBlockedCard()` via the
     existing `MutationButton`, reload after. Naming exactly what will be re-run.
   - Honest empty state ("No pending approvals."), error/offline via `Offline.load`
     + `LoadState` + `StaleBanner` (reuse `/v1/kanban/blocked` cache key).
   - A header line using the API's own `speak` one-liner.

2. **Integration & prominence:**
   - Add an **"Approvals" hub row** near the top of the Cluster tab (the fleet
     hub), distinctly from Board Hygiene.
   - Add a **`.badge(...)`** to the Cluster tab showing the pending-approval
     count (computed while the app runs) so pending approvals are visible at a
     glance — the phone-shaped "is there something needing me?" signal.
   - The count comes from a lightweight poll of `/v1/kanban/blocked` filtered to
     approvals.

3. **Voice:** `ApprovalsIntent` (Siri "check ${app} approvals") — speaks the
   server-derived count via the same `ApprovalsView` classification, read-only.
   Register in `AppShortcuts`. (Read-only, like ReviewQueueIntent — no confirm
   needed.)

## iOS verification (must all pass)

  * `bash ios-app/scripts/check_sources.sh` → exit 0 (new file listed in project.yml)
  * `bash ios-app/scripts/build_check.sh`  → exit 0 (full 3-target compile)
  * `bash ios-app/scripts/model_decode_check.sh` → exit 0 if models touched
  * No raw hex outside `Theme.swift`.

## Python / API verification

  * `scripts/run_tests.sh` green (hscc-api + hscc_daemon suites).
  * Tests must never write live operator state (no new state written).

## Honesty

  * There is NO iOS runtime on this host — no runtime claim. Compile-only +
    model-decode verification; state that clearly.
  * The approvals classification (kind-based) is client-side logic; it is
    documented in the view and matches the `kanban_block` tool's own kind
    semantics.
