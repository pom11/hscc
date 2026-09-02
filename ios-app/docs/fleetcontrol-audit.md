# FleetControlView audit — t_ecdfbfe5

Screen: `ios-app/Sources/HSCC/Views/FleetControlView.swift`
Endpoint: GET /v1/template/status (+ POST /v1/cluster/up, POST /v1/cluster/down)
Branch: wt/t_ecdfbfe5 (from dev)

## 1. DATA IN
- View fetches: `client.templateStatus()` → GET /v1/template/status (HSCCClient.swift:706-708). Live route answers 200 + parseable JSON (route sweep).
- Live body (recorded, host redacted):
  ```json
  { "applied": { "template": "4node-dual-dsv4", "applied_at": "2026-08-30T04:01:00",
      "orchestrator_node": "10.0.0.244", "families": ["reasoning"], "units": 2 },
    "note": "", "speak": "Template {'template': '4node-dual-dsv4', ...} is applied." }
  ```
- `units` here is an INT (2) → JSONValue .int → displayJSON shows "2". Model comment (Models.swift:877-880) says units may be int or {total, per_family}. View handles both via displayJSON.
- Does every field the view needs arrive? YES for the live shape. `orchestrator_node` NOTE: is a real LAN IP (10.0.0.244) — shown verbatim to operator.
- Cluster up/down: routes registered POST-only (GET→405 confirms). Client POSTs confirm:true (HSCCClient.swift:890-899).

## 2. RENDER
- speak shown italic (line 77-80) — raw Python repr, ugly but canonical.
- Template / Applied at / Orchestrator / Families / Units LabeledContent rows.
- units as int shows "2" (displayJSON line 159).

## 3. STATES — FINDINGS (in progress)
- loading → ProgressView (71-72)
- failed → HSErrorLabel red (73-74)
- loaded/applied==nil → HSEmptyLabel "No template applied." (97-99)
- loaded/applied!=nil → detail (75-96)
- **BUG A: `.stale` never produced + never handled.** loadStatus (54-59) uses plain .loading→.loaded/.failed, NOT Offline.load. Sibling TemplatesView (TemplatesView.swift:71-94) uses Offline.load with cacheKey EndpointPath.templateStatus and renders StaleBanner. So on a fetch failure with last-known data, FleetControlView shows a hard red error instead of last-known + offline banner — inconsistent, loses the offline-aware feature. Falls to default: EmptyView() (106-107) → empty Applied Template card. NOTE: empty (HSEmptyLabel) vs failed (HSErrorLabel) ARE visually distinct — the "0 vs failed" requirement is met; the gap is stale/offline.
- **BUG B (minor): `.idle` → default EmptyView()** — brief flicker before .task fires (loading set immediately).

## 4. CONTROLS
- Bring Fleet Up → MutationButton → confirm prompt → client.clusterUp() POST /v1/cluster/up confirm:true (HSCCClient.swift:890-892). Route registered (405 GET). Feedback: success/failure alert (MutationSupport.swift:73-84). Good.
- Stop All Workloads → MutationButton destructive → client.clusterDown() POST /v1/cluster/down confirm:true (897-899). Route registered. Destructive confirm names consequence. Good.

## 5. OBSERVATION
- No ObservableObjects in view; status is @State value enum → re-renders. No @StateObject-keyed-by-value issue. OK.

## 6. LAYOUT
- (in progress)

## 7. ACCESSIBILITY
- (in progress)

## FIXES
- (pending) BUG A: wire Offline.load + .stale + StaleBanner.
