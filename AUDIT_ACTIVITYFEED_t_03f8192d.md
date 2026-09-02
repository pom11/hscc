# Screen audit: ActivityFeedView (`t_03f8192d`)

Auditor: ios-engineer
Date: 2026-09-02
Scope: `ios-app/Sources/HSCC/Views/ActivityFeedView.swift` (+ backing client/model/route)
Live API: read-only, address derived from `hscc api status` (redacted to placeholder in this repo).

Task body asks 7 questions. Each answered with file:line evidence — executed
proof where possible, otherwise flagged as reasoning.

## 1. DATA IN — which endpoint(s), does every field arrive?  [EXECUTED]

Endpoint: `GET /v1/activity/feed?limit=N` — `HSCCClient.activityFeed()` at
`ios-app/Sources/HSCC/HSCCClient.swift:668-675` calls
`get(path: "/v1/activity/feed", queryItems:[limit], as: ActivityFeedResponse.self)`.
Route registered server-side at `hscc-api/routes_activity.py:297`
(`^/v1/activity/feed$`).

Live fetch (read-only) proved the envelope + every entry field the view needs arrive:
- Envelope keys `{entries, count, running_count, profiles, speak}` match
  `ActivityFeedResponse` (`Models.swift:1087-1093`) exactly.
- `ActivityEntry` (`Models.swift:1058-1069`) covers all 11 fields seen in live
  rows: `kind, profile, board, card_id, card_title, pid, host_local,
  started_at, at, tool, session_id` — 100% present in the live capture.
- Swift model decodes the real body: `live_decode_check.sh`
  → `v1_activity_feed.json → ActivityFeedResponse [POPULATED]`
  (33/33 live routes decode, 33/33 populated). NOT all-nil — so no silent
  empty-screen decode bug.
- Route answers: `api_route_sweep.py` → `/v1/activity/feed  200  parses`.
- `check_sources.sh` → 62 Swift files, all registered (ActivityFeedView.swift at
  project.yml:145). `build_check.sh` → HSCC 57 files, 0 errors, 0 warnings.

VERDICT: every field the view needs arrives and decodes. No dropped field at the
DECODE level.

## 2. RENDER — what does the operator see?  [EXECUTED + REASONING]

The view renders (all in `listSection`, ActivityFeedView.swift:80-118):
- A "Cluster Activity" header label (line 82).
- The server's `state.speak` line (line 92), italic secondary.
- The entry list (lines 96-106), each row via `entryRow` (120-160):
  - kind badge (Running=warn tint, Tool=ok tint) — line 168-176.
  - profile name, one-line-limited (line 127-129).
  - relative time `timeLabel(at)` (line 131-135, 180-191).
  - running row: "running <card_title> (<card_id>)" (line 137-141).
  - tool row: wrench icon + tool name (144-150) + "on <card_title> (<card_id>)" (151-155).

CONFIRMED RENDER BUG (count disagreement — the headline finding):
- Server `running_count = len(running_tasks)` (routes_activity.py:230) counts ALL
  running tasks, BEFORE the `entries[:limit]` cap (line 224).
- `count = len(entries)` (line 229) is the CAPPED count.
- So `running_count` (total tasks) can exceed the number of running rows that
  actually appear in the returned (capped) list. The `speak` line
  (`_speak_feed`, line 246-253) inherits the over-counted `running_count`.
- LIVE PROOF (capture `v1_activity_feed.json`, ts 20260902_211128):
  `running_count=3`, `count=50`, kinds in list = `{'tool_call': 50}`,
  **running rows visible = 0**. The view renders the speak line verbatim
  ("50 activity events across 3 running tasks.") yet the list shows ZERO
  Running badges. First live capture (ts 20260902_211128 was the second) showed
  `running_count=4`, 2 running visible. So the operator sees a header claiming
  N running tasks alongside a list showing fewer (or zero) "Running" badges —
  a genuine on-screen contradiction, reproducible under feed saturation.
- Root cause is server-side (running rows truncated by the tool-call cap), not a
  decode bug: the client faithfully renders `speak` + `entries`.
- Existing test `test_feed_limit_caps_entries` (test_routes_activity.py:246-254)
  only asserts `count <= limit` — it does NOT check `running_count` vs visible
  running rows, so the inconsistency is untested.

OTHER RENDER observations:
- Running row `at` = card `started_at` (routes_activity.py:182) → the relative
  time on a Running badge is "how long the card has been running", which is a
  reasonable read ("running for 2h"). Not a bug, but worth knowing: it is NOT a
  recent-activity time. [REASONING]
- `tool` namespaces are reduced to the head server-side (routes_activity.py:215),
  so `build_server.run → build_server` — matches the doc intent.

## 3. STATES — loading / empty / error / stale  [EXECUTED + REASONING]

`load(client)` (ActivityFeedView.swift:65-72) sets `.loading` then `.loaded` or
`.failed`. Rendered in `listSection` switch (84-110):

- loading → `ProgressView()` spinner (85-87). [code]
- empty (loaded, `entries` empty) → `emptyLabel("No agents running right now.")`
  tray icon, muted (97-99, 199-203). [code]
- failed → `errorLabel(message)` warning-triangle, Theme bad red (88-89,
  193-197). [code]
- idle (no fetch yet, client present) → `default:` → `EmptyView()` (108-109):
  just the "Cluster Activity" header, blank below, for the frame before `.task`
  swaps to .loading. [code]

DISTINCTION "0 results" vs "failed to load": these ARE visually distinct —
tray icon + muted text (empty) vs warning-triangle + bad-red (error). Good. [code]

STALE/OFFLINE: **NOT SUPPORTED** [EXECUTED]
- `load()` never produces `.stale` — it only sets `.loaded` or `.failed`.
- The view's switch does NOT handle `.stale` (only loading/failed/loaded
  explicitly + `default:` → EmptyView), so even though `LoadState` defines
  `.stale(Value, String)` (LoadState.swift:30), this view would render it as
  EmptyView if it ever occurred.
- Worse: the feed is never cached to StateCache — `get(path:queryItems:)` only
  stores when `queryItems.isEmpty` (HSCCClient.swift:260-262), but `activityFeed`
  ALWAYS passes the `limit` query item. So there is no persisted last-known state
  to fall back to, and `Offline.load` (LoadState.swift:120-145) would find
  nothing cached.
- CONSEQUENCE: a momentary network blip → `.failed` → the view DROPS whatever it
  had just shown and shows the error label. Compare: other screens use
  `Offline.load` to show last-known data with an age ("showing state from 6m
  ago"). This screen cannot. For a flaky-on-device operator this is the most
  likely failure mode: the feed blanks on a single missed request.

## 4. CONTROLS  [EXECUTED]

Controls in the view:
1. Pull-to-refresh — `.refreshable` (line 36) → `load(client)`. Route answers
   (sweep: 200, parses). Feedback: system refresh spinner + list updates. OK.
2. Tap a row — `NavigationLink` (121-123) → pushes `ActivityTraceView`.
   Feedback: new screen pushes. OK.
3. Auto-load on first appear — `.task` (37-41), gated to only run when
   `feed.value == nil && !feed.isLoading`. OK.

No button/toggle/swipe lacks feedback. No mute/favourite toggles exist.

## 5. OBSERVATION  [EXECUTED — no bug]

- The view holds `client: HSCCClient?` as a plain `let` (line 20) — HSCCClient is
  a struct (`HSCCClient.swift:114`), not an ObservableObject.
- The only mutable state is `feed` as `@State` (line 22). `@State` re-renders on
  change, so Point 5's "plain let doesn't re-render" bug does NOT apply here.
- No `@StateObject` exists, so no stale-instance-after-navigation bug.
- `feed`'s value (`ActivityFeedResponse`) includes `entries` which are value-type
  structs — no reference-type caching.
- VERDICT: observation is correct. There is no ObservableObject in this view's
  data path, so there is no "switch tabs to see it" bug here. [EXECUTED: source
  read; the reason it works is @State drives re-render]

On tab-return freshness: because `.task` only fires when `feed.value == nil`
(line 38), returning to this tab does NOT auto-refresh — the operator sees the
last data with no staleness marker until they pull-to-refresh. Not the
"switch tabs" bug (data IS present), but a freshness gap. [REASONING]

## 6. LAYOUT  [REASONING]

- `ScrollView` + vertical stack, `lineLimit(1)` on profile (129) — long profile
  names truncate rather than wrap. Time is `monospacedDigit` caption2 (133).
- Rows are VStacks; badge + profile + time in one HStack (125-136). On iPhone SE
  width (320pt) a long profile + badge + time could compress; `lineLimit(1)` +
  Spacer + trailing time should fit but the tool path with card ref (`on <title>
  (t_xxx)`) can truncate — captions wrap by default (no lineLimit on 152), so it
  wraps to 2 lines rather than hiding. Acceptable.
- Dynamic Type: `.headline`/`.subheadline`/`.caption2` scale with Dynamic Type.
  Badge uses fixed `.caption2.bold()` — scales. `timeLabel` caption2 scales.
  The trace view's `traceCard` uses `.body` — scales. No fixed point sizes that
  would break at XXXL except the notConfiguredView icon `.system(size:44)` — a
  fixed-size icon is a known a11y smell but not broken. [REASONING — no device]
- VERDICT: no hard truncation that hides meaning; SE-width and Dynamic Type
  survival is REASONED, not executed (no iOS runtime).

## 7. ACCESSIBILITY  [REASONING]

- The row's kind info is conveyed by BOTH a text badge (`kindLabel`: "Running"/
  "Tool", line 170) AND colour (warn/ok tint, line 169). Colour is NOT the only
  signal — good.
- `Image(systemName: "wrench.and.screwdriver")` at line 144 is decorative (paired
  with the tool text on line 147) — fine, text present.
- Tap target: whole row is a NavigationLink — large target. OK.
- No `.accessibilityLabel` needed since all icons are paired with text.
- VERDICT: no icon-only-without-label or colour-only defects found. [REASONING]

---

## What I FIXED

Server-side count consistency (the one clear broken thing):
- `hscc-api/routes_activity.py` `_build_feed` — running rows are now emitted
  OUTSIDE the `limit` cap (`entries = running_rows + tool_rows[:limit]`), so
  "who is running what is always visible" matches the route's own stated
  contract, and the `speak` line's `running_count` no longer disagrees with the
  visible Running badges on screen. Docstring updated to state `limit` caps
  tool-call entries.
- `hscc-api/tests/test_routes_activity.py`:
  - Added `test_feed_running_rows_survive_limit_cap` — regression asserting all
    `running_count` running rows are present under a saturated timeline, so the
    count can never over-state what the operator sees.
  - Updated `test_feed_limit_caps_entries` for the new contract (count = running
    + capped tool = <= limit + running_rows).
  - Added `_B_CARD` / `_C_CARD` fixtures.
- VERIFICATION (all executed):
  - `test_routes_activity.py`: 11 passed (incl. new regression).
  - `test_contract_swift_routes.py` + `test_api.py`: 42 passed (route registration
    intact).
  - Standalone `_build_feed` reconstruction on saturated fake input: 2 running
    cards + tool flood, `limit=10` → `count=12`, `running_count=2`, both running
    rows visible. Full `scripts/run_tests.sh` suite: 672 passed, 1 skipped,
    ALL GREEN across every package including hscc-api.

## What I deliberately did NOT fix

- iOS view is faithful: it renders `speak` + `entries` as the server sends them.
  No iOS-side change needed once the server stops truncating running rows.
- `.stale` offline state — left as-is; it's a broader offline-last-known feature
  gap, and adding it here is scope creep for an audit. Flagged for follow-up.
- `.idle` → EmptyView flash — negligible; `.task` fills it immediately.
