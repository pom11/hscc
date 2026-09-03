# Cron / scheduled-jobs view — API GAP report (t_c88094ef)

Date: 2026-09-03
Branch: cron-view-t_c88094ef
Assignee: ios-engineer

## Status: NO UI SHIPPED — the endpoint does not exist

Per the card: *"If no endpoint exposes cron state, record precisely what is
missing and stop. Do not invent one."*

The HSCC HTTP API — the only surface the iOS app talks to — has **no endpoint
that exposes cron job state** (no name/schedule/last-run/last-outcome for the
job list). Therefore a read-only cron view in the app **cannot be built**
without first inventing an API endpoint, which this card explicitly forbids.

This report records, precisely:
1. what cron data exists on the operator's host (so a future endpoint knows
   what is available to surface),
2. what the API exposes today (names-only, folded into autodown status),
3. exactly what is missing from the API contract,
4. the minimal endpoint shape that would unblock the iOS view.

---

## 1. What cron state EXISTS on the host (ground truth)

### `~/.hermes/cron/jobs.json` — the Hermes cron source of truth

Read on this host (2026-09-03): 11 jobs. Each job dict carries the fields the
view needs:

| field | example | present? |
|---|---|---|
| `id` | `bdf1af7e169e` | yes |
| `name` | `hscc-dep-watcher` | yes |
| `schedule_display` | `0 8 * * *` / `every 5m` | yes |
| `next_run_at` | `2026-09-04T08:00:00+03:00` | yes |
| `last_run_at` | `2026-09-03T08:00:44.775538+03:00` | yes |
| `last_status` | `ok` (also `error`, ...) | yes |
| `last_error` | null | yes |
| `last_delivery_error` | null | yes |
| `enabled` / `state` | `true` / `scheduled` (or `paused`) | yes |
| `profile` | (per-run agent profile) | yes |
| `repeat.completed` | 44 | yes |

Live active jobs on this host (2026-09-03, exactly the two the card names):
- `bdf1af7e169e` **hscc-dep-watcher** — `0 8 * * *`, last ok, last run
  `2026-09-03T08:00:44Z`
- `6407ea32e1dd` **hscc-escalate-watcher** — `*/15 * * * *`, last ok, last run
  `2026-09-03T13:45:53Z`

The reminder of the 11 jobs are paused (state `paused`, `enabled: false`).

### `~/.hermes/cron/executions.db` — per-run history

SQLite table `executions(id, job_id, source, process_id, pid,
process_started_at, status, claimed_at, started_at, finished_at, error)`. A
richer per-execution log than jobs.json (each run, not just the last). Not
exposed by any API either.

### Hermes agent knows how to read this (`hermes cron list`)

The `hermes` CLI (`/Users/desac/.hermes/hermes-agent/venv/bin/hermes`) resolves
the profile's cron store. But the HSCC API deliberately reads `jobs.json`
directly rather than shelling out (see `hscc_daemon/autodown.py:154-161`).

---

## 2. What the API exposes TODAY (the only cron datum)

A full enumeration of every registered route (from
`ROUTES.append(...)` across `hscc-api/*.py`) contains **no `/v1/cron*` route**
and no route returning schedule/last-run/last-outcome. The exhaustive route
list:

`/v1/ping`, `/v1/verify`, `/v1/commands`, `/v1/activity/feed`,
`/v1/autodown/{status,enable,disable,wake,cancel}`, `/v1/cards[+detail]`,
`/v1/cluster/{up,down,stop}`, `/v1/daemon/status`, `/v1/escalate`,
`/v1/kanban/{blocked,blocked/{id}/recover,stale,running,task/{id}/kill}`,
`/v1/memory[+edit+delete]`, `/v1/orchestrator/chat[+{id}]`,
`/v1/profile/{list,install,export,export/{file},editor/{profile}}`,
`/v1/profiles[+list+create+...]`, `/v1/projects[+detail+new+plan+standup+roadmap+...]`,
`/v1/review/queue[+detail+merge]`, `/v1/qa/queue`, `/v1/sessions[+retire+compact]`,
`/v1/standup`, `/v1/template/{list,status,preview/{name},apply}`,
`/v1/triggers[+run]`, `/v1/why/{card_id}`.

**The only cron datum reachable from the app** is folded into
`GET /v1/autodown/status` (registered `hscc-api/routes_autodown.py:379-380`,
handler builds them at `routes_autodown.py:238-239`):

```json
"active_cron_cpu_only": [ ...names of active cpu-only jobs... ],
"active_cron_model":    [ ...names of active model-requiring jobs... ]
```

These come from `autodown.list_active_cron_jobs()` which **already reads
jobs.json and returns full job dicts** (`{id, name, schedule_display,
next_run_at, no_agent, model, cpu_only}` — `autodown.py:189-235`), but the
status handler **throws away everything except the name**, keeping only the
cpu_only split (`routes_autodown.py:131-134`).

The app surfaces exactly that names-only pair in AutodownView's "Active cron
jobs" `HSSectionCard` (`ios-app/Sources/HSCC/Views/AutodownView.swift:303-327`).

---

## 3. Exactly what is MISSING from the API contract

To render the card's deliverable — *name, schedule, last run, last outcome* —
the API would need to expose, per job at minimum:

- `id`
- **`name`** — the card's "name"
- **`schedule_display`** — the card's "schedule"
- **`last_run_at`** — the card's "last run"
- **`last_status`** / **`last_error`** — the card's "last outcome"
- (`next_run_at` — useful, similar to kanban/stale next-run)

None of these are returned by any current endpoint. `/v1/autodown/status`
returns only the job NAME (and only for ACTIVE jobs, splitting cpu-only vs
model) — no schedule, no last run, no outcome, and paused jobs are invisible.

The executions.db run history (per-run status/started/finished/error) is also
not exposed anywhere.

---

## 4. Minimal endpoint to unblock the iOS view (NOT built — for the API owner)

A single read-only route, e.g.:

```
GET /v1/cron/list
```

returning an array of jobs, each:

```json
{
  "id": "bdf1af7e169e",
  "name": "hscc-dep-watcher",
  "schedule_display": "0 8 * * *",
  "enabled": true,
  "state": "scheduled",
  "next_run_at": "2026-09-04T08:00:00+03:00",
  "last_run_at": "2026-09-03T08:00:44+03:00",
  "last_status": "ok",
  "last_error": null
}
```

This maps 1:1 onto existing on-disk data (jobs.json) — *no new backend state,
no new collection* needed — and to the `autodown.list_active_cron_jobs()`
helper already in `hscc_daemon/autodown.py` (extended to carry `last_run_at`,
`last_status`, `last_error`, `enabled`). Read-only, no confirm gate.

**This is a backend (api/backend-engineer) card**, not an iOS card: the iOS
view is trivially blocked until a route exists. When it does, the iOS side is a
straightforward new `CronJob` model + a list view mirroring e.g.
`SessionsView`/`AutodownView` patterns, plus a `get("/v1/cron/list")` client
method.

---

## Evidence trail

- Exhaustive route enumeration: `grep ROUTES.append hscc-api/*.py` (this report
  §2) and the deduped route list produced during investigation.
- `autodown.list_active_cron_jobs()` already returns full dicts:
  `hscc_daemon/autodown.py:189-235`; CRON_JOBS_FILE: `autodown.py:163`.
- status handler keeps only names: `hscc-api/routes_autodown.py:131-134`,
  payload lines `routes_autodown.py:238-239`.
- App surfaces names-only in AutodownView: `ios-app/.../AutodownView.swift:303-327`.
- jobs.json fields verified by direct read on this host (2026-09-03).
