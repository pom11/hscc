# Screen audit: TemplatesView (t_18aefdb7)

Full audit of `ios-app/Sources/HSCC/Views/TemplatesView.swift` + its detail
screen (`TemplateDetailView.swift`, `TemplateTopologyView.swift`).

Status: COMPLETE. Live probes executed against the running API (read-only).

## EVIDENCE (executed)
- `bash ios-app/scripts/capture_live.sh` -> captured `/v1/template/list`,
  `/v1/template/status`, `/v1/template/preview/{name}` all HTTP 200.
- `bash ios-app/scripts/live_decode_check.sh` -> all three template responses
  DECODE+ [POPULATED] against the real models; "ALL 33 LIVE ROUTES DECODE AND
  CARRY REAL DATA" (this is executed proof the models match the wire).
- `bash ios-app/scripts/api_route_sweep.py` -> `ok 200` list/status, preview
  dynamic (probed 200 live), apply `post 405` (route exists, POST-only).
- `bash ios-app/scripts/build_check.sh` -> "full compile clean, 0 warnings".
- `bash ios-app/scripts/check_sources.sh` -> "sources in sync: 62 Swift files".
- pytest tests/test_routes_template.py + test_contract_swift_routes.py ->
  "19 passed".

## Risk note (address guard)
All live addresses redacted to `100.64.0.1` placeholder in this report. Real
token never recorded.

## 1. DATA IN
- GET /v1/template/list  -> HTTP 200 (route sweep: `ok   200`)
- GET /v1/template/status -> HTTP 200 (route sweep: `ok   200`)
- GET /v1/template/preview/{name} -> HTTP 200 (probed live for `4node-dual-dsv4`)
- POST /v1/template/apply -> route exists (POST-only; sweep reports `post 405`
  because it doesn't fire mutations; verified in hscc-api/tests + routes_actions.py)

Live `list` body: `{ "count": 14, "templates": [ 14 items ], "speak": "14
templates available." }`. Every `ClusterTemplate` field the view needs arrives:
`name, version, description, families, group` (Models.swift:865-873).

Live `status` body: `{ "applied": { "template": "4node-dual-dsv4",
"applied_at": "...", "orchestrator_node": "...", "families": ["reasoning"],
"units": 2 }, "note": "", "speak": "..." }`. Every field of `TemplateApplied`
arrives (Models.swift:888-894). Currently-applied template `4node-dual-dsv4`
IS present in the list, so the AppliedBadge will correctly attach to a real row.

## 2. RENDER
- Applied card (TemplatesView.swift:133-156): shows template name prominently
  (mono bold) + `state.speak` as caption.
- Library (lines 235-278): name, description, families ("Families: ..."),
  AppliedBadge when applied. All wired.
- DROPPED: `version` is never rendered in rows. Name-keyed so only one row per
  name; low impact today (all names distinct).
- FINDING: the applied card's secondary line is the server's raw `speak`,
  which for THIS server is a Python dict repr:
  `"Template {'template': '4node-dual-dsv4', 'applied_at': ..., 'orchestrator_node': '<host>', ...} is applied."`
  — leaks an internal address onto the operator's own screen and reads as a
  debug dump, not prose. The structured `applied` fields (applied_at, families,
  units) are what a human wants.

## 3. STATES (all distinct)
- status loading: ProgressView (line 108-109)
- status failed: HSErrorLabel red triangle (line 110-111)
- status stale: StaleBanner + last-known appliedBody (line 112-118)
- status loaded: appliedBody (line 119-120)
- list loading: ProgressView (line 166-167)
- list failed: HSErrorLabel + "Pull to retry..." (line 168-174)
- list stale: StaleBanner + last-known grouped/empty (line 175-185)
- list empty (0 rows success): HSEmptyLabel muted "No templates are available
  right now." + hint (line 187-193)
"0 results" (muted tray) and "failed to load" (red triangle) look clearly
different. Green.

## 4. CONTROLS
- Library row Button (line 236-239): sets selected + showDetail -> presents
  TemplateDetailView sheet. Feedback: sheet slides up.
- StaleBanner retry (line 114, 177): refetches status/list. Feedback: spinner
  swap.
- Pull-to-refresh (line 41): loadAll. Feedback: spinner.
- Detail Apply button (line 330-338): arms Confirm sheet (no request yet).
- Confirm sheet Apply (line 462-470): fires apply() -> phase=.reloading shows
  spinner + programming text. Real feedback.
- Apply POST backed by routes_actions.py:279 `handle_template_apply` -> calls
  `_backing_template_apply` -> cluster_template_cli. Client sends
  `confirm: true`; backend 409-gates it. Confirmed in routes.
All routes answer; controls have visible feedback. Green.

## 5. OBSERVATION
- TemplatesView holds NO ObservableObject. All state is `@State` (`status`,
  `list`, `selected`, `showDetail`). So the "plain let won't re-render" bug
  does not apply here.
- TemplatesView is a NavigationLink destination (ClusterView.swift:200-203),
  freshly constructed per push -> fresh `@State` each visit, no stale-instance
  bug. `let client: HSCCClient?` is stable across the push.
- TemplateDetailView all `@State` (lines 49-55). Same clean result.
- `let client` is NOT an ObservableObject, so it's fine.

## 6. LAYOUT
- Rows: HStack(top) with VStack of wrapping Text (no lineLimit) + Spacer +
  chevron. Name/description/families all wrap -> survive iPhone SE width.
- Topology blocks HStack (TemplateTopologyView.swift:18): widest template is
  8node (8 dots + 3 labels + spacing ~114pt) -> fits 320pt. Green.

## 7. ACCESSIBILITY
- AppliedBadge has .accessibilityLabel("Currently applied") (line 329).
- Row Button has .accessibilityHint (line 277); label = combined Text.
- StaleBanner retry has .accessibilityLabel("Retry loading") (Theme.swift:432).
- HSErrorLabel/HSEmptyLabel use `Label` (icon + text combined).
- Apply button has text label, not icon-only.
Mostly green.

## FINDINGS RANKED (by likelihood operator hits it)
1. (FIXED) Applied card showed raw Python dict `speak` (internal IP + debug
   repr) instead of clean applied metadata. Hit every time a template is
   applied — the applied card is the FIRST thing shown. Now renders structured
   `applied` fields (applied-at age · families · N units) via `appliedSummary()`
   with the dict-repr `speak` dropped. Fix at TemplatesView.swift:141-145,
   160-198. Compile-verified clean, and the underlying fields are live-decode
   POPULATED (TemplateStatusResponse). Reasoned change, but the new logic is
   exercised by compile + live models (NO iOS runtime here, so the rendered UI
   is reasoning, not an on-device screenshot).
2. (report) `version` not shown in rows. Low impact (names unique today).
3. (report) Cluster topology block HStack is not scrollable — could overflow on
   an extreme template (many families). All 14 live templates fit. Low risk.
