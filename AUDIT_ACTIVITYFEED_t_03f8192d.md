# Screen audit: ActivityFeedView (`t_03f8192d`)

Auditor: ios-engineer
Date: 2026-09-02
Scope: `ios-app/Sources/HSCC/Views/ActivityFeedView.swift` (+ backing client/model/route)
Live API: read-only, address derived from `hscc api status` (redacted to placeholder in this repo).

Task body asks 7 questions. Each answered with file:line evidence below — executed
proof where possible, otherwise flagged as reasoning.

## Evidence gathered so far (live, executed)

- `GET /v1/activity/feed?limit=50` (live, read-only) returns 200, JSON parses.
- Envelope keys: `entries, count, running_count, profiles, speak` — match
  `ActivityFeedResponse` exactly.
- Live values: `count=50`, `running_count=4`, `profiles=[backend-engineer, ios-engineer]`,
  `speak="50 activity events across 4 running tasks."`, `len(entries)=50`.
- Entry kinds in live sample: 48 `tool_call`, 2 `running`.
- All 11 entry fields present in live rows and all map 1:1 to `ActivityEntry`.

(Findings detail continues below.)
