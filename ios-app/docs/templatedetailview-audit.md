# Screen audit: TemplateDetailView — prove every element works

Task: t_6060f92b · branch `audit/templatedetail-t_6060f92b` · 2026-09-03
Scope: `ios-app/Sources/HSCC/Views/TemplateDetailView.swift` — template detail +
confirm-gated apply with post-apply reload polling. Apply is destructive, so
gating and feedback are the crux.

## Verdict
View is sound end-to-end. The one CLEARLY-broken defect was the error state's
"Pull to retry" copy advertising a gesture that did not exist (no `.refreshable`)
— FIXED. Everything else is verified-good or a deliberate non-fix (documented
below). The destructive apply is double-gated (confirm sheet in UI + HTTP 409
`confirm_required` server-side) — both gates proven live.

Addresses redacted to placeholders (`100.64.0.1` tailnet host; LAN nodes → `10.0.0.x`).
Real IPs seen in live data (`192.168.88.x`) are deliberately NOT reproduced here.

---

## 1. DATA IN — VERIFIED (executed)

Three read endpoints feed this view (all derived host, never hardcoded):

| Endpoint | Feeds | Client method |
|---|---|---|
| `GET /v1/template/list` | parent list → navigates here with a `ClusterTemplate` | HSCCClient.swift:702 |
| `GET /v1/template/preview/{name}` | this view's own load | HSCCClient.swift:717 |
| `GET /v1/template/status` + `GET /v1/verify` | post-apply reload polling | HSCCClient.swift:707, :484 |

Live fetch results (read-only):

```
GET /v1/template/list            → count: 14 templates; applied = 4node-dual-dsv4
GET /v1/template/status          → applied: {template: 4node-dual-dsv4,
                                    families: [reasoning], units: 2}
GET /v1/template/preview/4node-dual-dsv4 → speak "Preview: 5 changes."
                                    5 changes (write/write/update/create/provision)
                                    3 routing consumers (delegation, compaction,
                                    auxiliaries) + routing_untouched: []
GET /v1/verify                   → ok: true, 6 checks all ok
```

Every field the view reads decodes from the real server shape:
- `template.name`, `template.description`, `template.group`, `template.families`
  → all present in list rows.
- `preview.speak`, `preview.changes[]`, `preview.routing[]` → all present.
- `change.action/file/summary/details` → present (details: config.yaml has 2
  detail lines, proxies/ has 1). `change.diff_summary` IS in the server payload
  but the Swift model does not decode it (Models.swift:908 `TemplateChange`) —
  not rendered, not required.
- `route.consumer/target/model` → present on all 3 routing rows.

Decode proof (executed): `scripts/model_decode_check.sh` → `ALL DECODE CHECKS
PASSED — 48/48` including `template_list → TemplateListResponse`,
`template_status → TemplateStatusResponse`, `template_preview_hscc-live →
TemplatePreviewResponse`, `template_apply → TemplateApplyResponse`.
`scripts/check_sources.sh` → 62 Swift files all registered (`sources in sync`).

## 2. RENDER — VERIFIED, one dropped field (data-driven, low impact)

For the applied template the operator sees:
- **shape** (shapeSection:179-191) via `TemplateTopologyView` — orchestrator block
  (1 dot) + family block (3 dots). Caption "4 nodes — families: reasoning".
  NOTE: this split is an APPROXIMATION derived from `group`/`families`
  (TemplateTopologyView.swift:55-90, self-documented); the exact split lives in
  the preview's change `details`, which ARE shown below it. Not a bug — documented.
- **preview** (previewContent:225-256) — "Config changes" (5 `changeRow`s) then
  "Workload routing" (3 `routingRow`s). Each change shows ACTION verb +
  `file` + `summary` + indented `details` lines. Plain `Text` preserves the
  leading whitespace in detail lines (`  orchestrator: …`) — operator sees them
  indented as intended.
- **counts**: server `speak` = "Preview: 5 changes." ↔ exactly 5 rendered change
  rows. NO client/server count disagreement.

DROPPED FIELD: `routing_untouched` is decoded (Models.swift:945) but NEVER
rendered — `previewContent` (226-256) shows only `changes` + `routing`.
Semantically meaningful (consumers the apply leaves untouched). Live value is
`[]`, so zero current impact. Deliberate non-fix (see Findings).

## 3. STATES — VERIFIED; loading/empty/error distinct

- **loading**: `ProgressView()` in previewSection (202). ✓
- **empty** (success, zero rows): `changes.isEmpty && routing.isEmpty` →
  speak label + "No detailed preview is available for this template yet. You
  can still apply it…" (229-238). Distinct from error. ✓
- **error**: `errorLabel(message)` + "Couldn't load the preview. Pull to retry,
  or try again later." (203-208). After my fix the pull-to-retry is REAL.
  Distinct from empty. ✓
- **stale/offline**: NONE for preview. `loadPreview` (103-107) calls
  `client.templatePreview` directly, not through `Offline.load` (LoadState.swift:120).
  The preview path IS cached by StateCache (single-arg `get`, HSCCClient.swift:223),
  but the view never reads it back on failure → a failed refresh shows `.failed`,
  no last-known fallback. Deliberate non-fix (below).

## 4. CONTROLS — VERIFIED (all routes answer, all have visible feedback)

| Control | Location | Calls | Route answers? | Visible feedback |
|---|---|---|---|---|
| Done | toolbar 77-79 | `dismiss()` | n/a | sheet/screen closes |
| Apply (main) | 330-340 | sets `showApplyConfirm=true` (arms sheet only, NO request) | n/a | sheet presents |
| Apply (sheet) | 461-470 | `onApply` → `Task { apply() }` | POST /v1/template/apply — routes_actions.py:328, **live 409-proof** | `phase=.reloading` → reloadingSection (60-62) replaces content |
| Force recreate toggle | 441-450 | binds `forceRecreate` → sent as `force_recreate` in apply body (HSCCClient.swift:770-773) | yes (routes_actions.py:283 reads it) | toggle state + explained caption |
| Cancel (sheet) | 459 | `dismiss(); onCancel()` | n/a | sheet closes |

Apply gating — **PROVEN live, read-only** (no mutation performed; a missing
`confirm` is rejected before anything runs):
```
POST /v1/template/apply  {"name":"4node-dual-dsv4"}            → HTTP 409
POST /v1/template/apply  {"name":"4node-dual-dsv4","confirm":false} → HTTP 409
  body: { error: { code: confirm_required, message: "this action is
  destructive and requires \"confirm\": true…" } }
```
So a bare single tap can never apply. The client ALWAYS sends `confirm: true`
(HSCCClient.swift:770); the HTTP gate is a second, independent defence.

Post-apply feedback (116-174): apply returns → `phase=.reloading` shows
reloadingSection (spinner + "Applying <name>…" + "fleet is reloading…"). Then
`pollReload` polls `/v1/template/status` + `/v1/verify` every 5s up to 9 min;
on healthy return → `.applied` banner "…applied — the fleet is back up."
On failure/timeout → `.degraded(<message>)` banner in red. Real, visible feedback.
`onDisappear` cancels the poll (86), `onApplied` lets the parent list refresh (142).

## 5. OBSERVATION — VERIFIED (no re-render bug)

All state is `@State` value types (LoadState enum + bools + phase) — **no
ObservableObject** is held at all, so no `let`-vs-`@StateObject` trap applies
(lines 49-55). `client` and `template` are `let` structs (HSCCClient is a
struct, HSCCClient.swift:115; ClusterTemplate is a struct) — value types, no
re-render issue.
- Presented as a `.sheet` from TemplatesView (88-98) → a fresh instance per
  navigation. Not keyed by a changing value (no dynamic container), so no
  stale-first-instance bug. ✓

## 6. LAYOUT — VERIFIED (low risk)

`ScrollView` + `VStack`, all rows/`Text` wrap (`lineLimit` nowhere set). Vertical
stacking survives iPhone SE width. `TemplateTopologyView` uses an `HStack(spacing:_20_)`
of blocks — for an 8-node 2-family template that's 3 blocks (~12 dots @ 8px +
links) ≈ could approach ~300px but compactly; dots are small. Dynamic Type:
caption/subheadline scale, no fixed heights. Low risk.

## 7. ACCESSIBILITY — VERIFIED (good)

- changeRow/routingRow: `.accessibilityElement(children: .combine)` with real
  text labels (284, 308). Apply button has text+icon; Done/Cancel have text.
- Color as ONLY signal: `changeActionColor` (311-318) colors the action verb by
  type — but the verb is ALSO shown as text (`change.action.uppercased()`, 261),
  so not color-only. routingRow target uses ok-green but has "→" text too.
  Topology dots are color+shape (dots with links), not color-only. ✓

---

## What I fixed
1. **`.refreshable` — dead "Pull to retry" copy (CLEARLY BROKEN).**
   Error state (206) said pull to retry; the ScrollView had no `.refreshable`.
   Added one (TemplateDetailView.swift:86-90) that re-fetches only the read-only
   preview, never a re-apply — matching the sibling-view pattern.
   Proof: `build_check.sh` → `full compile clean, 0 warnings`; change is the
   only delta from `dev` (verified `git diff dev --name-only`).

## Deliberate non-fixes (why)
1. **No offline/stale fallback for preview.** The preview is a point-in-time
   dry-run of what applying would change, dependent on the fleet's CURRENT
   state. Showing stale dry-run data "from 6m ago" for a DESTRUCTIVE action is
   more dangerous than a clear failure + real pull-to-retry. Weakening the
   honesty of the destructive-action surface for the sake of the offline feature
   is the wrong trade. (LoadState.swift supports `.stale`, but the apply surface
   should not).
2. **`routing_untouched` not rendered.** Currently always `[]` (zero impact),
   and a third "Routing untouched" section would clutter the apply screen for a
   case that does not occur in the fleet. Not worth it now.

## Findings ranked (by how likely the operator hits it)
1. **HIGH — was: "Pull to retry" dead copy** (error state advertised a gesture
   that didn't exist) → **FIXED** (add `.refreshable`, TemplateDetailView.swift:86).
   This was the one bug a real-device tester would hit as soon as a preview fetch
   failed.
2. **LOW — `routing_untouched` never rendered** (Models.swift:945 decoded,
   previewContent:225-256 drops it). Live `[]` → zero current impact. Deliberate
   non-fix.
3. **LOW — no offline fallback for preview.** Deliberate non-fix (destructive
   surface should show live truth or a clear failure, not stale dry-run).
4. **INFORMATIONAL — topology shape is approximate** (TemplateTopologyView.swift).
   Self-documented; exact split shown in preview details. Not a bug.

## Evidence commands (all executed)
- `hscc api status | sed -n 's/.*Listening: *\([0-9.]*:[0-9]*\).*/http:\/\/\1/p'` — derived host
- `curl GET /v1/template/list|status|preview/…|verify` — live values above
- `curl -X POST /v1/template/apply` without/with `confirm:false` → HTTP 409s (gate proof)
- `bash ios-app/scripts/build_check.sh` → `full compile clean, 0 warnings`
- `bash ios-app/scripts/model_decode_check.sh` → `ALL DECODE CHECKS PASSED — 48/48`
- `bash ios-app/scripts/check_sources.sh` → `sources in sync: 62 Swift files`

## Proof vs reasoning
EXECUTED (compile / decode / live fetch / gate): sections 1, 3, 4, and the fix.
REASONING (no iOS runtime, structural read only): sections 2 render details,
5, 6, 7 — checked against source + models but not rendered on a device.
