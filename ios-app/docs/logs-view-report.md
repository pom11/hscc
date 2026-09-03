# Report: t_2eda26a6 — Logs: tail a worker or the daemon from the phone

Status: BLOCKED (no API surface to build on)
Assignee: ios-engineer
Date: 2026-09-03

## Headline finding

**The HSCC HTTP API exposes NO log endpoint.** There is no route that returns daemon,
API, or worker log lines. Therefore this card CANNOT be implemented as specified, and per
the card's own security instructions ("If the available log endpoint returns secrets, say
so on this card and stop") we are stopping rather than shipping a fabricated or unsafe
feature.

Logged agent hours confirmed: the only "logs" the API surfaces is a single quiet line
counting how many log messages were suppressed during rate-limited connection
reconciliation (see below). There is no raw log content available over HTTP.

## Investigation trail

### 1. Confirmed the complete API surface
- `hscc-api/` — no file matched a log/tail/logs/log_entries pattern; 0 results.
- `hscc_daemon/` — 26 files contain "log" but all are internal Python logging
  (`logging.getLogger`, `self.log`, `logger.debug`) or log-file rotation/cleanup in the
  daemon core. None are HTTP routes.
- The HTTP API router (FastAPI in `hscc-api` / `hscc_daemon/hscc.py`) exposes routes for
  daemon status, stats, health, autodown, kanban cards, chat, fleet, memory, sessions,
  activity/feed, projects, and jobs — but NOT logs.

### 2. The one "log" string exposed by the API
The only place log text reaches an API consumer is a single COUNT of suppressed messages,
not their content:

```
hscc_daemon/stats.py (fleet/stats endpoint):
  "rate_limited_logs_suppressed": <count>,
```

This is a scalar counter, not log content. It cannot be used to build a read-only log view
because it carries no log lines, no timestamps, no source, no message body.

### 3. Why we cannot just "add a route"
The task is assigned to ios-engineer (the iOS app owner). Adding a new HTTP endpoint to the
daemon is backend work owned by a different role, and — critically — we could not test it
against a live daemon from this environment. Shipping an iOS log view that calls a route
that does not exist would produce a permanently erroring screen. Doing so would be
dishonest ("shipped a log view" with no backend) and would not meet the deliverable.

## What would be needed to unblock

A backend change (owner: backend/daemon role), e.g.:

```
GET /v1/logs?source=daemon|api|worker&limit=200
```

Returning the last `limit` lines, bounded (never a full file), with the daemon already
redacting tokens/hosts/session-ids before serving, OR a documented endpoint that the iOS
client then redacts on display.

Security boundary confirmed for the future view:
- iOS must redact hostname/Tailnet addresses → replace with `100.64.0.1`.
- redact tokens/session ids.
- never write log content into audit reports or commits.

## Recommendation for next step
Create a backend card: "Daemon: add bounded redacted `GET /v1/logs` endpoint". Once that
exists and is verifiable, this card can be re-opened for the iOS log view UI. The iOS UI
design (LogsView with pull-to-refresh, bounded tail, redaction client-side) is spec'd in
this repo so the follow-on implementation is mechanical.
