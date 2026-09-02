# Screen Audit: FleetView (`ios-app/Sources/HSCC/Views/FleetView.swift`)

Task t_806756e4 · ios-engineer · worktree `ios-app/` (branch `audit/fleetview-t_806756e4`)

Live API address: `http://<tailnet-host>:8788` (REDACTED — derived at runtime via
`hscc api status`, never hardcoded). Token: `~/.hscc/api-token`.

## Methodology
- Read FleetView.swift end-to-end; read the 5 response models + the 5 HSCCClient methods.
- Fetched all 5 live GET responses read-only (curl + real token) and recorded values.
- Ran `scripts/api_route_sweep.py` — every route FleetView calls answers 200 + parses.
- Ran `scripts/live_decode_check.sh` — every FleetView model decodes the REAL live
  capture and is POPULATED (not all-nil).
- Ran `scripts/fleet_offline_check.sh` (NEW) — compiles the REAL LoadState.swift
  (Offline enum) and proves .stale/.failed/.loaded semantics.
- Ran `scripts/build_check.sh` — full compile of all 4 targets after the fix, 0 errors 0 warnings.
- NO iOS runtime here. Findings marked (EXECUTED) are proven by command output;
  (REASONING) are code inspection. Stated plainly per finding.

## Point 1 — DATA IN (EXECUTED)

Endpoints (HSCCClient.swift): health()=431, fleetStats()=436, fleetThroughput()=443,
fleetStreams()=448, autoscale()=453. All 5 answer 200 + parse (api_route_sweep.py):
```
ok 200 /v1/health          ok 200 /v1/fleet/stats
ok 200 /v1/fleet/throughput  ok 200 /v1/fleet/streams  ok 200 /v1/autoscale
```
live_decode_check.sh: 33/33 live routes decode, ALL 5 FleetView models POPULATED
(HealthResponse, FleetThroughputResponse, FleetStreamsResponse, FleetStatsResponse,
AutoscaleResponse). Every field the view renders arrives.

Real values (EXECUTED, captured at scripts/live_captures/20260902_211149):
- health: ok:true, 6 checks all ok. speak "All checks passed."
- stats: total 33, by_profile {} (EMPTY), by_day {08-27:7,08-28:8,08-29:15,08-30:1,09-01:2},
  activity.tool_calls_by_profile {test:162,unknown:21}, top_tools [[test_tool,162],[terminal,14],...].
  speak "About 33 work items across the last 7 days."
- throughput: fleet {prompt_tokens:55473917, generation_tokens:301750, running:1, waiting:1,
  nodes_ok:1, nodes_total:2}; by_node {<redacted LAN url>:{...}}. speak "1 of 2 nodes healthy."
- streams: watchdog/heartbeat/dispatcher/triggers/proxy/idle/... each {ok:true, timestamp, message}.
- autoscale: action "none", reason "within healthy band".

## Point 2 — RENDER (what the operator sees)

Each section renders its `speak` one-liner + the typed fields:
- **Health**: speak line + per-check rows (name + detail). Renders all 6 checks. ✓
- **Throughput**: speak + badges nodes-ok, prompt, generation, running, waiting (FleetView.swift:181-196). All aggregate fields rendered. ✓
- **Stats**: speak + "work items" badge + "By profile" sub-list (FleetView.swift:236-257). `by_profile` is EMPTY in live data, so only the 33 badge + speak line appear. **DROPPED: `by_day` (populated! 7,8,15,1,2) and `activity` (tool_calls_by_profile, top_tools) are never rendered.** The operator sees "33 work items" but NOT the trend that activity spiked to 15 on 08-29 and dropped to 1-2 in the last two days — hides real meaning. (REPORT, not fixed)
- **Streams**: speak + per-stream rows (icon, name, timestamp). **FIXED: `stream.message` ("Pipeline healthy", "dispatcher healthy — no genuine stall") is now rendered** (FleetView.swift:293). Before this audit it was silently dropped — the operator saw a green dot and a time but not WHY. This is the human-readable health detail; added.
- **Autoscale**: speak + reason. ✓
- Client-side counts agree with the server's own: throughput shows "1/2 nodes ok" matching server nodes_ok/nodes_total; stats "33 work items" matches completions.total — the count the view shows IS the server's count (not a client recompute). The one server-internal discrepancy (throughput fleet.nodes_total=2 but by_node has 1 entry) is a SERVER-side artifact the view faithfully reports; it does not disagree with itself. (REASONING + EXECUTED live values)
- Units: tokens are raw (55473917) — no K/M/G formatting. Large but not wrong; `fmt` shows integer when whole, 1dp when fractional. (REPORT, cosmetic)

## Point 3 — STATES

Each section has its own LoadState (one degraded endpoint never blanks the screen).
- **loading**: `ProgressView()` spinner per section. ✓
- **empty** (success, zero rows):
  - Health: checks.isEmpty → HSEmptyLabel "No health checks reported." (FleetView.swift:127)
  - Throughput: fleet==nil → "No throughput data." (197)
  - Stats: completions==nil → "No stats reported." (255)
  - Streams: streams.isEmpty → "No daemon streams reported." (289)
  - Autoscale: none (always shows speak).
- **error**: `HSErrorLabel` (red `exclamationmark.triangle`, FleetView.swift:107/159/212/261/321). 
- **"0 results" vs "failed" LOOK DIFFERENT**: ✓ empty = muted `tray` icon; error = red `exclamationmark.triangle`. Never identical. (REASONING, from the shared components Theme.swift:354-373)
- **stale/offline**: **FIXED.** Previously FleetView had NO stale path — it set `.loaded`/`.failed` directly, so on a network blip with cached data it showed all-5 "failed" (cluster looks DOWN/idle). Now every section routes through `Offline.load` (FleetView.swift:53-101) and renders `.stale` with `StaleBanner(age:reason:)` + the last-known body (e.g. :108-114). Proven by fleet_offline_check.sh: failed+cached → `.stale("showing state from 6m ago")`; failed+nothing → `.failed`. This is the same class of bug the task flags ("the cluster looked idle"); FleetView was the ONLY fleet screen that lacked offline handling.

## Point 4 — CONTROLS

FleetView has **no mutating buttons, toggles, or swipe actions** — it is a pure read
surface. The only controls:
- **Pull-to-refresh** (`.refreshable { await loadAll() }`, FleetView.swift:34) → re-runs
  all 5 GETs. Feedback: spinner + refreshed values. Routes answer (sweep above). ✓
- **StaleBanner retry** (arrow button, now present in every `.stale` state) → re-runs that
  one section's load. Visible feedback (icon). ✓
- Back navigation (NavigationStack default), section cards are static content, not tappable.
Nothing here calls a POST; there is no route-answer risk from a mutating control.

## Point 5 — OBSERVATION

All five states are `@State private var ... = LoadState<...>.idle` (FleetView.swift:16-20),
value types, NOT ObservableObjects. No `ObservableObject` is held, so there is no
plain-`let`-won't-re-render bug and no `@StateObject` keyed-by-changing-value stale
instance. FleetView is pushed fresh each navigation (ClusterView.swift:194
`FleetView(client: client)` in a NavigationLink destination), so `@State` is
re-initialized per visit — no "I had to switch tabs" staleness. ✓ Clean.
(SwiftUI re-renders on `@State` mutation because `LoadState` is Equatable-free value
type — the computed `body` re-evaluates on each assignment. REASONING.)

## Point 6 — LAYOUT

- `ScrollView` + `VStack(alignment:.leading)` → vertical scroll, all sections reachable. ✓
- `statBadge` rows: first HStack holds 3 badges (nodes-ok, prompt, generation), each
  `.frame(maxWidth:.infinity)` (~107pt on SE). "55473917" is 8 digits at `.title3.bold()`
  (~20pt) ≈ 88pt — fits on SE (375pt) but is the tightest element. PROMOTE TO REPORT:
  at larger Dynamic Type the 3-badge HStack will wrap awkwardly, but does not crash or
  hide data. (REASONING)
- Uses semantic `.font(.body/.subheadline/.caption)` everywhere → scales with Dynamic Type.
  No fixed heights that clip. Not clearly broken. (REASONING)

## Point 7 — ACCESSIBILITY

- Health check rows & stream rows use an SF Symbol icon (`checkmark.circle.fill` vs
  `xmark.circle.fill`) whose GLYPH SHAPE differs — colour is NOT the only signal. ✓
- Icon-only leading elements have no explicit `.accessibilityLabel`, but each is paired
  with adjacent `Text(name)` so VoiceOver reads the name. NOT colour-only. Low priority.
- `StaleBanner` retry button has `.accessibilityLabel("Retry loading")` (Theme.swift:431). ✓
- Labels/titles are all real `Text`/`Label`, no colour-as-only-signal for meaning-critical
  states (empty/error/stale each have distinct icons + text). ✓ Acceptable.

## FIXES MADE
1. **F1 (HIGH): offline/stale handling.** FleetView now routes all 5 sections through
   `Offline.load` (FleetView.swift:53-101) and renders `.stale` with `StaleBanner` +
   last-known body (5 places). Before: only `.loaded`/`.failed` → offline meant
   "cluster looks down/idle". This matches the design-system standard every other
   fleet screen already used.
   - Proof: fleet_offline_check.sh compiles the REAL LoadState.swift; offline+cached →
     `.stale("showing state from 6m ago")`; offline+nothing → `.failed`. PASS.
   - Compile: build_check.sh clean (0 err, 0 warn, all 4 targets).
2. **F3 (streams.message): render the human-readable stream health detail.** The
   `message` field ("Pipeline healthy", "dispatcher healthy — no genuine stall") was in
   the payload+model but never shown. Now rendered under each stream name
   (FleetView.swift:295-301). The operator now sees WHY a stream is ok, not just a dot.

## REPORTED, NOT FIXED (ranked by how likely the operator hits them)
- **R1 (HIGH-ish, deliberate):** Stats `by_day` and `activity` are dropped — the operator
  sees "33 work items" but not the 7/8/15/1/2 daily trend or tool counts. This is real
  meaning hidden. Not fixed because rendering them is a nontrivial UI addition (a day
  chart / activity table) and the speak line already carries the headline; flagging for a
  follow-up card rather than a rushed UI change. by_profile is rendered but is EMPTY in
  live data — so for the operator today, the Stats section is just a number + a sentence,
  which under-represents what the endpoint returns.
- **R2 (MED):** `/v1/fleet/stats` is never persisted to StateCache because fleetStats()
  always sends `?days=7` and the cache-store is gated on `queryItems.isEmpty`
  (HSCCClient.swift:260-261). With my Offline.load fix, stats still falls back to
  in-session last-known (Offline.load falls back to `current.value`), so it's NOT a blank
  after a blip mid-session — but it will NOT survive relaunch like the other 4 do.
  Fixing requires either caching the query variant or a client change; out of scope for a
  view audit, flagged.
- **R3 (LOW):** Throughput `by_node` (per-node metrics) dropped — operator sees aggregate
  only, not which node is waiting. The `by_node` key is a raw LAN URL (redacted above);
  rendering it would show a network address. Low value, flagged.
- **R4 (LOW):** Large token counts unformatted (55473917). Cosmetic.
- **R5 (LOW):** 3-across stat badge row is tight at iPhone SE width + large Dynamic Type.
  Cosmetic wrap, no data loss.

## DELIBERATELY NOT FIXED & WHY
- by_day/activity render (R1) — real meaning hidden but UI design is a decision for the
  operator; a quick bad chart is worse than the honest "speak line + count". File as a
  child task.
- fleetStats cache persistence (R2) — touches client-level caching policy; out of scope
  for a view audit.
- by_node render (R3) — would print a LAN address to a screen; low value.
- I did NOT change the fetch paths or endpoint routes — all 5 routes verified alive and
  unchanged.

## EVIDENCE (file:line + commands)
- Routes answer: `python3 scripts/api_route_sweep.py` → 5 fleet routes all `ok 200`.
- Live values: `bash scripts/capture_live.sh` → scripts/live_captures/20260902_211149/.
- Decode/population: `bash scripts/live_decode_check.sh scripts/live_captures/20260902_211149`
  → "33/33 decoded, 33/33 populated" incl. all 5 FleetView models.
- Offline semantics: `bash scripts/fleet_offline_check.sh` →
  "PASS offline WITH cached value → .stale (msg: showing state from 6m ago) /
   PASS offline with NO value → .failed / ALL OFFLINE SEMANTICS PASS"
- Compile after fix: `bash scripts/build_check.sh` →
  "full compile clean, 0 warnings (all 4 targets)".
- Source registration: `bash scripts/check_sources.sh` → "62 Swift files, all listed".
- Fix sites: FleetView.swift:16-20 (@State), :53-101 (Offline.load in all 5 loaders),
  :108-114 etc. (.stale + StaleBanner), :293 (stream.message).

## STATUS
- All 7 audit points answered with file:line evidence.
- Fixed: offline/stale handling (HIGH), stream.message render.
- Reported (not fixed): by_day/activity, stats cache persistence, by_node, formatting, layout wrap.
- Commits: 8995924 (scaffold), 9228b8f (fix + harness).
