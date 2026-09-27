# t_ca133e7d — Phase 4 [decode-drift] recheck audit

Contract §5 leg: recheck EVERY Codable model in ios-app/Sources/HSCC against the
LIVE response from the RUNNING API (not route source, not memory); fix drift;
add a contract-pinning fixture/test per family so the next drift fails loudly.

## Outcome

The API was UP for this run (PID 43760, listening on the tailnet — base resolved
at runtime, never committed). Live captures were taken twice: the first
(34 routes) to survey, then a full 37-route capture after extending the capture
set. **No decode drift was found** in any covered model — all 36/36 live routes
decode AND carry real data (population check, not just shape). Three
previously-uncovered wire families were pinned with new fixtures + checks,
closing the last coverage gaps.

## Model → endpoint → drift → fix → guarding test/fixture

Legend: drift found → y/‑n. Fix column empty when no client change was needed
(the model already matched the live wire).

### Previously-uncovered families (NEW fixtures + checks this phase)

| model | endpoint | drift | fix | guarding |
|---|---|---|---|---|
| `CommandsResponse` / `SlashCommand` | `GET /v1/commands` | n | — | `fixtures/commands.json` + `main.swift` c.check + contract assertion |
| `ProfileListResponse` / `ProfileSummary` | `GET /v1/profiles/list` | n | — | `fixtures/profiles_list.json` + c.check + contract assertion |
| `LogsResponse` / `LogEntry` | `GET /v1/logs?source=daemon&limit=N` | n | — | `fixtures/logs_daemon.json` + c.check (bare `[LogEntry]`) + contract assertion |
| `ApiErrorBody` / `ApiErrorEnvelope` | (error envelope) | n | — | existing APIError.swift decode path (not a response-model) |

### Families already covered — re-verified live this run (no drift)

| model | endpoint | drift |
|---|---|---|
| `PingResponse` | `/v1/ping` | n |
| `HealthResponse` (also `/verify`) | `/v1/health`, `/v1/verify` | n |
| `ClusterStatusResponse` (Shared) | `/v1/cluster/status` | n |
| `ClusterHostsResponse` | `/v1/cluster/hosts` | n |
| `ClusterMonitorResponse` + nested | `/v1/cluster/monitor` | n |
| `ReadResponse` (jobs/info bucket) | `/v1/cluster/jobs`, `/v1/cluster/info` | n |
| `CardsResponse` / `CardDetailResponse` / `Card` | `/v1/cards`, `/v1/cards/{id}` | n |
| `StandupResponse` | `/v1/standup` | n |
| `ReviewQueueResponse` / `ReviewDetailResponse` | `/v1/review/queue`, `/v1/review/{id}` | n |
| `QAQueueResponse` / `QARow` / `ManualQARow` | `/v1/qa/queue` | n |
| `FleetStatsResponse` / `FleetThroughputResponse` / `FleetStreamsResponse` | `/v1/fleet/*` | n |
| `AutoscaleResponse` | `/v1/autoscale` | n |
| `AutodownStatusResponse` (Shared) + 4 mutations | `/v1/autodown/*` | n |
| `ProjectsResponse` / `ProjectDetailResponse` | `/v1/projects`, `/v1/projects/{name}` | n |
| `DaemonStatusResponse` | `/v1/daemon/status` | n |
| `TriggersResponse` | `/v1/triggers` | n |
| `EscalationsResponse` | `/v1/escalate` | n |
| `ProfilesResponse` / `ProfileEditorResponse` | `/v1/profiles`, `/v1/profile/editor/{p}` | n |
| `KanbanBlockedResponse`/`BlockedCard` (+why) | `/v1/kanban/blocked` | n |
| `KanbanStaleResponse` / `StaleCard` | `/v1/kanban/stale` | n |
| `ActivityFeedResponse` / `ActivityEntry` | `/v1/activity/feed` | n |
| `SessionHistoryResponse` (+ all 7 `ParsedPayload` cases) | `/v1/projects/{n}/session/events` | n |
| `TemplateList/Status/PreviewResponse` + nested | `/v1/template/*` | n |
| `SessionsListResponse` / `SessionItem` | `/v1/sessions?profile=` | n |
| `CronListResponse` / `CronJob` | `/v1/cron/list` | n |
| `HistoryResponse` / `HistoryEvent` (+ kinds/outcomes) | `/v1/daemon/history` | n |
| `OrchestratorChatJobResponse` / `OrchestratorChatJobStatus` / `ChatJobError` | `/v1/orchestrator/chat`, `/{id}` | n |
| Mutation POST responses | see fixtures README | n |

### Decodable models that are NOT wire responses (out of scope by design)

- `ReadResponse` — self-contained generic `{ speak, payload? }` bucket, no
  dedicated fixture needed (existing, documented).
- `SavedCluster` (Shared) — local persistence `Codable`, not a live API body.
- Notify `ObservedState` / `AnnouncedState` / `LastSeenState` — local app state
  persisted to disk, never decoded from the API.

## Changes (this phase)

- `scripts/model_decode_check/main.swift` — +3 `c.check` rows and +3 contract
  assertion blocks (commands / profiles_list / logs_daemon). 56 → **62/62**.
- `scripts/model_decode_check/fixtures/commands.json` (new) — live command
  catalog.
- `scripts/model_decode_check/fixtures/profiles_list.json` (new) — live
  full-roster shape (10 keys, always-null distribution_*), 4 representative
  profiles.
- `scripts/model_decode_check/fixtures/logs_daemon.json` (new) — live daemon
  tail, 6 lines, INFO+ERROR, all 4 keys per row.
- `scripts/model_decode_check/fixtures/README.md` — provenance + coverage
  updated (Phase 4 additions).
- `scripts/capture_live.sh` — add `/v1/commands`, `/v1/profiles/list`,
  `/v1/logs?source=daemon&limit=50` to the live capture set (34 → 37 routes).
- `scripts/live_decode_check/main.swift` — decode the 3 new families; total
  33 → **36/36** populated.
- `scripts/compare_fixtures_live.py` — route_map entries for the 3 new routes.

## Notes on the remaining compare_fixtures_live "DIFF" rows

`scripts/compare_fixtures_live.py` reports 16 shape diffs against the live
capture. Every one is a **value/state difference** (the live system sits in a
different operational state than the committed canned fixtures), NOT decode
drift — proven because `live_decode_check` decodes all 36/36 routes populated.
Concretely: empty QA queue and blocked list right now, busy-day fleet stats
with different dates, live sessions with `title:null`, `daemon pid:null`, etc.
The models' optionals already tolerate every one of these — no fixture value was
changed to force a green; the shapes match the live wire as captured.

## Verify (all run against the working tree)

- `bash scripts/model_decode_check.sh` → **ALL DECODE CHECKS PASSED — 62/62**.
- `bash scripts/live_decode_check.sh scripts/live_captures/20260927_030108` →
  **36/36 decoded, 36/36 populated.**
- `python3 scripts/compare_fixtures_live.py scripts/live_captures/20260927_030108`
  → 16 shape diffs, all state/value-level (see above), new families all OK.
- Both harnesses compile the REAL model sources — no model redeclared, so a
  green here is the real contract.

Addresses scrubbed to 100.64.0.1 (the listening host only ever appears via
`hscc api status` at capture time, never committed). No secrets. No AI
attribution. Repo public — notes in docs/audits/, fixtures sanitized value-only.
