# PHASE 3 — MISSING SURFACES: PRIORITISATION NOTE (2026-09-26)

Purpose: for each missing capability, decide (a) worth an API route + iOS surface,
(b) API route only, or (c) deliberately CLI-only, with justification. Operator value
and safety decide, not completeness.

## Measurement (verified on origin/main 2026-09-26)

Breadth: the app reaches 25 of 27 API route families; no dead calls (every /v1/... the app
calls exists server-side). The real gap is DEPTH — the `hscc project` ~35 subcommands and
the CLI verbs with no `/v1` route.

KEY RE-VERIFICATION (corrects the contract's earlier framing — the API side has moved on):
- `/v1/cron/list` — ALREADY EXPOSED server-side (hscc-api/routes_cron.py, GET /v1/cron/list,
  read-only roster of all scheduled jobs). Registered in api_server.py. iOS references: 0.
  There is even ios-app/docs/cron-view-gap.md saying the iOS cron view is "blocked on" this
  endpoint — the endpoint now exists, so the iOS view is UNBLOCKED.
- `/v1/why/{card_id}` — ALREADY EXPOSED server-side (routes_project.py:1073, GET /v1/why/{card_id}
  — the card's full story: kanban + git facts). iOS references: 0.
- `/v1/logs` — exposed (routes_logs.py). iOS references: 0 (but a stranded branch
  audit/logs-t_2eda26a6 adds an iOS Logs view — being reconciled in Phase 1).
- `/v1/daemon/history` — exposed (routes_history.py). Branch audit/history-t_b5ce7935 has an
  iOS SelfHealHistory view (Phase 1 reconciliation).

So for the two contract-named families (cron, why), the API route already exists. The genuine
missing work is the iOS SURFACE, not the API route.

## Prioritisation

### 1. /v1/cron + iOS cron view — TOP PRIORITY (a) route exists + iOS surface
- Justification: operator cares about crons (this 24h run is itself kept alive by one). The
  read-only route already exists, iOS coverage is 0. A cron roster view (name, schedule,
  deliver target, next run, last run, enable/disable) is high operator value.
- Decision: (a) iOS surface consuming the existing GET /v1/cron/list. Route already done.
- Mutating cron operations (create/edit/pause) would be confirm-gated — FUTURE CARD, not now.
  Start with the read-only roster view.

### 2. /v1/why/{card_id} + iOS card-story view — HIGH (a) route exists + iOS surface
- Justification: `why` gives the card's full story (kanban + git facts) — directly useful for a
  card/session inspector in the app. Route already exists. iOS coverage 0.
- Decision: (a) iOS surface. Route already done.

### 3. daemon start/stop — (a) API route + iOS surface, but HIGH SAFETY BAR
- Justification (contract §4c): on 2026-09-25 the daemon died repeatedly and could NOT have been
  recovered from the phone. This is a real operational gap. A stranded branch
  (audit/servingcontrol-t_e13c8c2b) has per-unit stop/restart control — being reconciled in
  Phase 1 with mandatory CONFIRM-GATING.
- Decision: (a) but MUST be confirm-gated exactly like `template apply`. Never bind 0.0.0.0.
  Fleet-mutating; the confirm-gated serving-control surface from Phase 1 is the vehicle. This
  overlaps Phase 1 t_c14a427c.

### 4. check / notify — (a) API route + iOS surface (operator actions)
- Justification: `check` (validate/probe a unit or the app) and `notify` (notify operator) are
  genuine operator actions with no API path. `notify` pairs with the notify-operator surface
  being reconciled in Phase 1. `check` is read-only-ish and useful from the phone.
- Decision: (a). `check` is safe (read/heal); `notify` is safe (it notifies — confirm-gate not
  strictly required but keep it non-destructive). Lower priority than cron/why.

### 5. Remaining hscc project subcommands (decompose, doctor, hygiene, migrate, metrics,
   monitor, roadmap, standup(has route), review(has route), template(has route), etc.)
- Large, heterogeneous CLI surface. Most are either already exposed (qa/review/standup/
  why/projects/sessions) or are heavy operational commands better run from a terminal.
- Decision: (c) deliberately CLI-only for the long tail. Do NOT attempt to expose 35
  subcommands. Only the operator-value surfaces above merit routes.

### 6. Deliberately CLI-only (local setup) — (c) never remote
- install, uninstall, plist (local packaging/launchctl). Never remote. No route.

## Recommended execution order (time-boxed)
1. iOS cron roster view consuming GET /v1/cron/list  — STRONGEST, route ready.
2. iOS card-story view consuming GET /v1/why/{card_id} — route ready.
3. Confirm-gated daemon serving control (by way of Phase 1 serving-control branch landing
   confirm-gated). API + iOS.
4. check / notify routes + surfaces — if time remains.

If Phase 1 or the board saturates, the cron view is the one to land no matter what.
