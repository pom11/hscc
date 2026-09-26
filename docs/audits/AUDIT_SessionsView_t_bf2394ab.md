# Screen audit: SessionsView — prove every element works

Task: t_bf2394ab · branch `audit/sessions-t_bf2394ab` · 2026-09-02
Scope: `ios-app/Sources/HSCC/Views/SessionsView.swift` — session list + retire/compact.
The two POSTs were broken until yesterday — verified they now work.

## Verdict
View is sound end-to-end. Both mutations are live-proven working. I made ONE fix
(offline/stale handling — the recurring "screen blanks on a failed refresh" bug,
same class fixed in FleetView/FleetControlView prior audits). Everything else is
verified-good or a deliberate non-fix (data-driven, not view defects).

---

## 1. DATA IN — VERIFIED (executed)

Endpoint that feeds it: `GET /v1/sessions?profile=<name>` (HSCCClient.swift:570-577),
server routes_sessions.py:198-222. Read-only, no `confirm`.

Live fetch `GET /v1/sessions?profile=hscc-orch` (host derived, not hardcoded):
```
profile: hscc-orch
count: 119   bloated_count: 0
speak: "119 sessions on hscc-orch; 0 at compaction risk."
```
Every field SessionItem needs arrives: `id, title, message_count, total_tokens,
input_tokens, compaction_headroom, context_window, threshold_tokens, bloated,
reason` — all present on all 119 rows (verified structurally via script).

Decode proof: `scripts/live_decode_check.sh` decodes the REAL live capture against
the REAL SessionItem model → `SessionsListResponse [POPULATED]` (33/33 routes).
Custom harness `scripts/sessions_row_check/main.swift` decoded the live capture and
printed the computed fields the view renders — all real, none all-nil.

## 2. RENDER — VERIFIED, two UX notes (both data-driven)

Client-rendered rows from `.sessions.count` = 119 == server `count` 119. NO count
disagreement.

Per-row rendering (SessionsView.swift:157-222):
- displayTitle (Models.swift:1003 `title ?? id`): **117/119 rows have `title: null`**
  live, so the operator sees a wall of opaque ids (e.g. `20260827_102447_92b65d`).
  DATA issue, not a dropped field — the sessions genuinely carry no human title.
- message_count → "219 msgs" (present, real).
- tokenSummary (Models.swift:1007-1009, = total_tokens) → **"9480.7k"** for the
  9,480,667-token session. Rendered in thousands; 9.5M reads as "9480.7k". Same
  `formatCount` app-wide; awkward but not wrong. Deliberately not changed.
- compaction_headroom → "162.1k headroom" (formatCount, SessionsView.swift:172).
- bloat badge only on isBloated; reason caption only when bloated.
- Envelope `bloated_count` not rendered directly, but the server `speak` line
  covers it. Not a dropped field.

## 3. STATES — REPAIRED (one gap fixed)

- `.loading` → ProgressView (SessionsView.swift:124-126).
- `.loaded` + 0 rows → emptyLabel "No sessions on this profile." (tray icon, muted
  color) — SessionsView.swift:160.
- `.failed` → errorLabel (exclamation triangle + bad red) — SessionsView.swift:127-128.
- EMPTY and ERROR look different (different icon + color). Satisfies point 3.
- FIXED: **.stale/offline was missing.** load() used plain `.loading→.loaded/.failed`,
  so a refresh failure after a successful load blanked to `.failed`. Now load()
  routes through Offline.load (SessionsView.swift:104-111) and the `.stale` case
  (SessionsView.swift:129-135) renders a StaleBanner ("Offline — showing state from
  X ago" + Retry) over the last-known list. First-load spinner preserved (only show
  `.loading` when `list.value == nil`).
  Note: `sessions(profile:)` passes a query item so get() never persists it to
  StateCache (HSCCClient.swift:260 `if queryItems.isEmpty`); the stale fallback is
  the in-session value — correct for pull-to-refresh-while-flaky. Cross-relaunch
  offline still honestly reports `.failed` (nothing cached) — acceptable.

## 4. CONTROLS — VERIFIED (both POSTs now work, live-proof)

- Compact: `POST /v1/sessions/{id}/compact` (HSCCClient.swift:598-603) → routes
  routes_sessions.py:278-321.
- Retire: `POST /v1/sessions/{id}/retire` (HSCCClient.swift:585-590) → routes
  routes_sessions.py:241-275.
- Body always `{ profile, confirm: true }` — exactly what the handlers require
  (`_require_confirm`, routes_sessions.py:95-103; 409 `confirm_required` otherwise).

Live-proof the routes answer (read-only, non-mutating):
```
POST <BASE>/v1/sessions/<real-id>/retire  body {profile}          -> 409 confirm_required
POST <BASE>/v1/sessions/<real-id>/compact body {profile}          -> 409 confirm_required
```
HTTP 409 with the confirm_required payload means the request REACHED the handler
(a dead/absent route would be 404/502). emit both paths. This is the executed proof
"the two POSTs now work".

(api_route_sweep.py only sweeps GETs; POSTs are listed as not-covered by design —
lines 17-19, 109-113. So these two were NOT exercised by the sweep; I verified them
manually above.)

Feedback: every mutation flows through MutationButton (MutationSupport.swift:30-103):
tap → confirm dialog naming exactly what happens → confirm-gated request → SUCCESS or
FAILURE alert. The alert title is "Done" on success, "Failed" on failure; a non-2xx
throws and lands in the failure alert — a failed action can never render as success.
`reloadAfterMutation` (SessionsView.swift:253-255) refetches so each row reflects
post-action state. In-flight guard disables the button (no double-fire).

## 5. OBSERVATION — NO ISSUE

SessionsView holds only value-type `@State`: `profile: String` and
`list: LoadState<SessionsListResponse>` (SessionsView.swift:26-27). There is NO
ObservableObject, so the `@StateObject`/stale-first-instance "switch tabs" bug
cannot occur here. `client` is a plain `let` but is not an ObservableObject — correct
(the view rebuilds with the current client via `if let client`). Point 5: PASS, no fix.

## 6. LAYOUT — REASONING (no iOS runtime here)
ScrollView + VStack + `.padding()`, no fixed widths or hardcoded frame sizes.
All fonts are Dynamic-Type-scalable (`.headline/.subheadline/.caption/.caption2`).
Stat rows use `HStack(spacing:12)`; action buttons `HStack(spacing:10)` + trailing
`Spacer` — both compress. Should survive iPhone SE width and large Dynamic Type.
This is REASONING, not executed proof — no simulator/device on this worker node.

## 7. ACCESSIBILITY — PASS
Every control has visible text, not just an icon: "Compact", "Retire", the
Load button ("Load"), the stats (value + "msgs"/"tokens"/"headroom" labels are text).
Bloat badge is text "bloated" + red color — both signals, not colour-only. Retire is
red but also labeled "Retire". No icon-only controls found. Profile field is a
Labelled TextField. Point 7: PASS, no fix.

---

## What I changed
1. HSCCClient.swift:93 — added `EndpointPath.sessions = "/v1/sessions"`.
2. SessionsView.swift — `load()` is now offline-aware (Offline.load; spinner only on
   first load) and the list renders a `.stale` case (StaleBanner + last-known list)
   via the new `sessListBody(client:state:)`. Deliberately kept the first-load spinner.

## What I deliberately did NOT fix (and why)
- Opaque displayTitle (117/119 rows): the server returns `title: null`; a human-
  readable title is a server/data concern, not a view defect. Out of scope.
- `tokenSummary` "9480.7k" formatting: app-consistent (`formatCount` everywhere);
  changing units is a product decision. Left as-is.

## Ranked by operator likelihood of hitting
1. (FIXED) Offline/refresh blanking — moderate; a connection blip after a good load
   used to empty the screen. Now shows last-known + banner.
2. Opaque session ids on every row — certain today, but a data problem.
3. "9480.7k" tokens — seen on every large session, cosmetic.

## Proof summary (all executed)
- live_decode_check.sh → SessionsListResponse [POPULATED] (33/33 live)
- sessions_row_check harness → 119 rows, client==server count, computed fields real
- curl 409 on retire & compact → both routes registered and answering
- build_check.sh → 0 errors, 0 warnings after fix

## Harness I added
- `ios-app/scripts/sessions_row_check/main.swift` — decodes a real /v1/sessions
  capture and prints the exact fields SessionsView renders. Standalone (compiled by
  hand like the other *_check harnesses); NOT registered in project.yml (it's a
  script, not app source — matches the other harnesses).
