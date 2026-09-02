# Screen Audit: FleetView (`ios-app/Sources/HSCC/Views/FleetView.swift`)

Task t_806756e4 · ios-engineer · worktree `ios-app/` (branch `audit/fleetview-t_806756e4`)

Live API address: `http://<tailnet-host>:8788` (REDACTED — derived at runtime via
`hscc api status`, never hardcoded). Token: `~/.hscc/api-token`.

## Methodology
- Read FleetView.swift end-to-end (330 lines).
- Read the 5 response models it renders (Models.swift) + the 5 HSCCClient methods it calls.
- Fetched all 5 live GET responses read-only and recorded real values.
- Ran `scripts/api_route_sweep.py` — every route FleetView calls answers 200 + parses.
- No iOS runtime here: compile + source-registration + decode + logic via harnesses.
  Findings marked (EXECUTED) are proven by command output; (REASONING) are code inspection.

## Point 1 — DATA IN
Which endpoints feed it (HSCCClient.swift):
- `client.health()`        → GET /v1/health                    (HSCCClient.swift:431)
- `client.fleetStats(7)`   → GET /v1/fleet/stats?days=7        (HSCCClient.swift:436)
- `client.fleetThroughput()`→ GET /v1/fleet/throughput         (HSCCClient.swift:443)
- `client.fleetStreams()`  → GET /v1/fleet/streams             (HSCCClient.swift:448)
- `client.autoscale()`     → GET /v1/autoscale                 (HSCCClient.swift:453)

All five answer 200 + parseable JSON (EXECUTED, api_route_sweep.py):
```
ok   200  /v1/health
ok   200  /v1/fleet/stats
ok   200  /v1/fleet/throughput
ok   200  /v1/fleet/streams
ok   200  /v1/autoscale
```

Live values observed (EXECUTED):
- **health**: ok:true, 6/6 checks ok (plugins, multiplex, daemon_streams, proxy,
  config_wiring, profile_endpoints). speak="All checks passed."
- **stats**: since_days 7, completions.total=33, by_profile={} (EMPTY),
  by_day={08-27:7, 08-28:8, 08-29:15, 08-30:1, 09-01:2},
  activity={tool_calls_by_profile:{test:162,unknown:21},
  top_tools:[[test_tool,162],[terminal,14],[memory,4],[tool_call,1],
  [kanban_create,1],[kanban_show,1]]}. speak="About 33 work items across the last 7 days."
- **throughput**: fleet={prompt_tokens:55473917, generation_tokens:301750, running:1,
  waiting:1, nodes_ok:1, nodes_total:2},
  by_node={http://10.0.0.247:8000/metrics:{...same...}}. speak="1 of 2 nodes healthy."
  (REDACTED the LAN node URL — real node address in evidence.)
- **streams**: 9+ daemon streams (watchdog, heartbeat, dispatcher, triggers, proxy,
  idle, gateway...), each {ok:true, timestamp, message:"..."}. speak present.
- **autoscale**: action:"none", reason:"within healthy band". speak="Autoscale: nothing to change."

## Point 2 — RENDER (what the operator sees)  [DRAFT — filling in]
## Point 3 — STATES  [DRAFT — filling in]
## Point 4 — CONTROLS  [DRAFT — filling in]
## Point 5 — OBSERVATION  [DRAFT — filling in]
## Point 6 — LAYOUT  [DRAFT — filling in]
## Point 7 — ACCESSIBILITY  [DRAFT — filling in]

## FINDINGS SO FAR
- F1 (HIGH, fixing): FleetView does NOT use `Offline.load` — it sets `.loaded`/`.failed`
  directly (FleetView.swift:51-78). It is the ONLY fleet view that bypasses the
  offline-aware path (every other screen does: OpsView:67, Templates:72, Projects:163,
  Cluster:76, Autodown:74, Approvals:195, BoardHygiene:214, Search:268). On a network
  blip with cached data the operator sees all-5 "failed" → cluster looks DOWN/idle.
  This is the same class of bug as the dead chat pipeline the task warns about.
- F2 (report): /v1/fleet/stats is never cached because fleetStats() always sends
  ?days=7 and the cache store is guarded by queryItems.isEmpty (HSCCClient.swift:260-261).
  So even with the Offline.load fix, stats has no offline fallback → still "failed" offline.
- F3 (report): dropped fields hiding meaning — stats by_day (7,8,15,1,2) and activity
  (tool_calls, top_tools) never rendered; throughput by_node never rendered; streams
  `message` (the human-readable WHY) never rendered.

## FIXES
- [in progress] F1: route FleetView loads through Offline.load + render .stale with StaleBanner.
- [in progress] F3 (streams.message): render the stream `message` under each stream row.

## Evidence commands
(captured above in transcript)
