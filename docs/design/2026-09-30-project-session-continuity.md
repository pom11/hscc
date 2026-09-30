# Design — Project Session Continuity Across Surfaces (Approach 1)

**Date:** 2026-09-30
**Status:** Draft for operator review
**Scope:** All HSCC projects (Option A — project-tied sessions)
**Approach:** Approach 1 — the project's named Hermes session is the single source of truth; the CLI/REPL and the iOS app are two clients on that one session.

## 1. Problem

When the operator chats in a Hermes session on this machine for an HSCC project, and
separately opens that project in the HSCC iOS app, they should be the **same
conversation** — same session id, full history on both surfaces, live two-way.

Today the pieces exist but are not joined into that continuum:

- Each project owns a **named Hermes session** (`hermes -p <profile> chat -Q --continue <named>` in `routes_orchestrator.py`).
- `hscc-api/gateway_driver.py` connects a project out to a running `hermes serve` gateway (two WS: `/api/pty` to type, `/api/events` to receive) and translates the native event stream into a per-project `SessionEventStore`.
- `routes_session.py` + `routes_ws.py` serve that store to the iOS app (history paging + live WS relay), one contiguous seq space.

Gaps for full Option-A continuity:

1. **Same-session pinning** — the driver must resolve to the project's exact named
   session id so the CLI REPL and iOS live on literally the same session, not two
   streams of "the project."
2. **Full-history backfill** — the store is currently fed by live events only. Opening a
   project on iOS must show the entire history of the named session up front, not only
   what accumulated since the bridge connected.
3. **iOS→machine direction** — a message sent on the phone must land in that same named
   session and be visible (full history) when the operator resumes on the machine.

## 2. Architecture

The project's **named Hermes session is the single source of truth.** Whichever surface
is actively driving runs the session's turn loop; the other surface live-views the same
conversation from the shared store.

```
   this machine                     hscc-api daemon                   iOS app
  ┌──────────────┐            ┌──────────────────────────┐      ┌──────────────┐
  │ hermes REPL  │            │ gateway_driver connects  │      │              │
  │ (CLI / hscc  │  typed msgs│ to hermes serve, targets │      │  open project│
  │ project chat)│───────────►│ the project's NAMED       │◄─────┤  → GET history│
  │              │◄───────────│ session id               │      │  → WS live   │
  └──────────────┘  events    └─────────────┬────────────┘      └──────┬───────┘
                                            │                          │ send
                                 SessionEventStore (per project)       │
                                            ▲                          │
                                            └────── store frames ──────┘

   session identity: project -> named hermes session id (in the profile's state.db)
   driving: only one surface runs the turn loop at a time; other surface live-views
```

Key properties:

- **Identity = the project's named session id.** No new session model; no divergent store.
- **Hermes stays the owner** of the session (compaction, unread, agent layer all keep
  working). hscc-api only relays and mirrors, never becomes a second source of truth.
- **One contiguous seq space per project** in the store spans history + live, so the iOS
  reconnect contract (gap-free/dup-free) is preserved.

## 3. Components & Data Flow

### 3.1 Same-session pinning (gateway_driver / routes_orchestrator)
- Add a resolution step: for a given project, resolve its **named session id** from the
  owning profile's `state.db` (the same seam `routes_orchestrator` already uses for
  `--continue`). The driver connects to `hermes serve` targeting that exact id.
- Both the CLI chat path and the WS-relay path go through this resolver, so they always
  agree on which session a project is.

### 3.2 Full-history backfill (gateway_driver)
- On connecting a project, backfill the `SessionEventStore` from the named session's real
  history in `state.db` before opening the live feed — so iOS shows the whole conversation
  immediately, not just events since connect.
- Backfill is idempotent (keyed on the session id + store high-water mark) so reconnects
  don't duplicate.

### 3.3 Two-way relay (gateway_driver / routes_ws)
- App→session: the WS `send` frame is relayed into the same named session via the driver
  (`send_user_message` already exists).
- Session→app: live events translate to store frames as today.
- Session→machine CLI: when the operator resumes the named session in the REPL, they see
  everything — including messages that originated on the phone — because it's the same
  session (`--continue <named>`).

### 3.4 All projects
- The resolver is generic over any registered project; no per-project special casing.

## 4. Error handling

- **Session resolution failure** (project has no session yet): `404 / not_found` consistent
  with every other `/v1/projects/{name}` endpoint; the project may not have chatted yet.
- **Backfill failure while live events flow**: log + continue live; iOS already has
  reconnect semantics, so a later reconnect retries the backfill.
- **Contention** (project's named session busy with kanban work): reuse the existing
  `_backing_busy_tasks` signal — report honestly rather than wedge silently.
- **Driver disconnect**: existing reconnect logic (t_218cb9ec) preserves gap-free/dup-free.

## 5. Testing

- **Backfill determinism**: unit-test the backfill against a temp state.db with a known
  session history; assert the store high-water matches, no dupes on re-backfill.
- **Session resolution**: unit-test the resolver against a temp profile state.db; unknown
  project → 404.
- **Live two-way**: the existing `tests_gateway/probe_*` harness pattern — a real
  `hermes serve` probe proving a message typed on the app side reaches the session and,
  after `--continue` on the CLI, shows up with full history.
- **Contract**: iOS decode accepts a backfilled-history page identical in shape to live.

## 6. Scope guardrails

- iOS stays **paused** (per operator, will resume). This design's iOS touch is limited to
  consuming the already-specified history+live contract — no new iOS work until resume.
- Backend/hscc-api work can proceed (it's not the iOS app track).
- No new session model; Hermes remains the owner of every session.

## 7. Open questions for operator

- Confirm the CLI "machine-side" chat path is `hscc project chat` / `hermes --continue`
  against the named session (Assumption: yes, that's the existing seam).
- Pilot-first vs straight-to-all: decision made — **all projects**. No pilot gate.

## 8. Deliverables

- Backend: same-session pinning + full-history backfill + two-way relay, all projects.
- Tests: backfill determinism, resolution, live two-way probe, contract.
- iOS: consume the backfilled history + two-way (deferred until iOS resumes).
