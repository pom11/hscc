# Screen Audit: CardsView (t_cb300f9c)

Scope: `ios-app/Sources/HSCC/Views/CardsView.swift` (the kanban card detail screen).

> NOTE: the file `CardsView.swift` contains ONE struct, `CardDetailView`, the
> per-card read surface reached by tapping a card in `ProjectBoardView`
> (the actual board/list lives in `ProjectsView.swift`, audited separately in
> t_806756e4). This audit covers the detail view exactly as the task scopes it.

## Context
- Reviewed struct `CardDetailView` (CardsView.swift:8-68).
- Live capture set: `ios-app/scripts/live_captures/20260902_222848/` (33 routes).
- All addresses redacted to the documented placeholders (tailnet -> 100.64.0.1;
  local path prefix -> ~).

## 1. DATA IN — endpoint, live values, field coverage
- Endpoint: `GET /v1/cards/{id}` via `HSCCClient.cardDetail(_:)`
  (HSCCClient.swift:402-405) → decodes `CardDetailResponse`
  (Models.swift:184).
- LIVE PROOF: `curl GET /v1/cards/t_2472675d` -> **HTTP 200**, full JSON body.
  Capture file `v1_cards_detail.json` (live_captures/20260902_222848) contains
  these keys: id, title, body, status, assignee, board, branch, priority,
  created_at, started_at, completed_at, last_heartbeat_at, workspace_kind,
  workspace_path, speak.
- The route is also live-answered by the sweep (api_route_sweep.py lists
  `/v1/cards` at 200; the interpolated `/v1/cards/{id}` is hit directly here).
- DECODING (compiled real models, live capture):
  `DECODE+ v1_cards_detail.json -> CardDetailResponse [POPULATED]` (33/33
  routes decode + populated).

## 2. RENDER — what the operator sees; dropped fields
- The view renders a speak Label, then a "Card" section with ID / Title /
  Status rows (CardsView.swift:23-33).
- **BUG (FIXED): 8 of the 12 fields the live endpoint returns were dropped.**
  The detail endpoint returns `body` (the card description), `assignee`,
  `board`, `branch`, `priority`, `created_at`, `started_at`, `completed_at`,
  `last_heartbeat_at`, `workspace_kind`, `workspace_path` — but
  `CardDetailResponse` declared only `id/title/status/speak` (Models.swift:184)
  and the view rendered only those (CardsView.swift:28-31).
  The BIGGEST loss is `body` — for a "card detail" screen the description is the
  primary content, and it was completely invisible. A tapped card showed only
  metadata + a one-line synthesized `speak`, never the actual card contents.
  Confirmed: the committed fixture `card_detail_t_049d6986.json` ALREADY carries
  body/assignee/board — the model was discarding data both live and in fixture.
- No wrong units / truncation found otherwise; all displayed values are raw
  strings passed through unchanged.
- There is NO client-side count shown.

## 3. STATES — loading / empty / error / stale
- loading: `HSLoading("Loading…")` (CardsView.swift:35) while `card == nil`.
- loaded: List shown (CardsView.swift:22-33).
- error: `HSError("Couldn't load card", …)` full pane with retry (CardsView.swift:18-20).
- empty: n/a — single-resource fetch, no "zero rows" meaning. A card always exists.
- stale/offline: **NOT handled** (see below).
- DISTINCT: "failed to load" (HSError pane) is visually distinct from any loaded
  state; there is no empty-vs-error conflation (no empty state exists).
- GAP (REPORTED, not fixed): the view does NOT use the app's `LoadState` /
  `Offline.load` pattern (LoadState.swift) that `ProjectBoardView` and the other
  audited screens use for offline last-known state. It uses a plain
  `@State card: CardDetailResponse?` + `@State loadError: HSCCError?`. If the
  cluster drops after the card was shown, the user gets the full error pane
  instead of last-known stale data. This is a genuine inconsistency but not a
  crash; the error is honest and distinguishable. Reported, not fixed (see
  "deliberately not fixed").

## 4. CONTROLS — every button/swipe
- Retry ("Try again") on the HSError state (CardsView.swift:20) → `Task { await load() }`.
  LIVE-REACHABLE: load() re-fetches via cardDetail; the route answers 200. Proof
  that the control calls a live-routed method: the same load() path succeeded in
  the live capture. Visible feedback: yes, it re-renders the pane.
- Pull-to-refresh: **NOT present** on this detail view (the board `ProjectBoardView`
  has `.refreshable`, CardsView does not). Minor, reported not fixed.
- Tapping a card in the board navigates via `NavigationLink { CardDetailView(cardID:) }`
  (ProjectsView.swift:702-704) — so reaching THIS view is proven (NavigationLink is
  wired, cardID passed as a stable `let`).

## 5. OBSERVATION — @StateObject/@ObservedObject keyed-by-value
- This view holds NO ObservableObject. It owns plain `@State` for card/loadError/
  isLoading (CardsView.swift:12-14), which is correct for a single-fetch view.
- `cardID` is a `let` (CardsView.swift:10) — stable, never a @StateObject-key problem.
- No "stale first instance after navigation" risk here: state resets naturally
  per screen (plain @State, no object identity to go stale).
- The "I had to switch tabs to see it" bug class does NOT apply to this view
  (it is pushed navigation, and the data arrives via the same screen's `.task`).

## 6. LAYOUT — Dynamic Type / small screen
- Content is a `List` of `LabeledContent` rows + scrollable Text sections → scales
  with Dynamic Type, wraps, and fits SE width (no fixed-width frames).
- The rendered body Text uses `font(.hsccMono(13))` — wrapped, selectable,
  scrollable. No truncation of the description.
- Reasoning (no iOS runtime available): List + LabeledContent + Text are the
  standard adaptive stack; no custom frames or forced sizes that would overflow
  a 375pt screen.

## 7. ACCESSIBILITY
- All rows are `LabeledContent(label) { Text(value) }` — VoiceOver reads the label.
- The speak Label has `systemImage` + a text label → has an accessible name.
- NEW: body Text has `.textSelection(.enabled)` → copyable, and it is readable text.
- No icon-only control with no label: the only button is the titled "Try again".
- Colour is NOT the only signal: status is plain text, not colour-coded.
  HSError uses an exclamationmark.triangle icon + text, not colour alone.

## What was fixed
1. `Models.swift:184-193` — extended `CardDetailResponse` with `body`, `assignee`,
   `board` (all present on the live endpoint and the committed fixture). Optional,
   so old captures/fixtures still decode.
2. `CardsView.swift:32-40` — render a "Description" section showing the card body
   (mono, selectable), and add Assignee + Board rows to the Card section.
3. `live_decode_check/main.swift` — added a targeted regression assertion that the
   live detail response carries body/assignee/board, so the view can't silently
   drop the description again. PASSES against the real live capture.

## Executed proof
- compile: `build_check.sh` -> "full compile clean, 0 warnings" (57 HSCC files).
- source registration: `check_sources.sh` -> "sources in sync: 62 Swift files".
- decode (live): `live_decode_check.sh` -> "LIVE DECODE: 33/33 decoded, 33/33
  populated; ALL 33 LIVE ROUTES DECODE AND CARRY REAL DATA" + new
  "CardDetailResponse carries body/assignee/board" OK line.
- decode (fixtures): `model_decode_check.sh` -> "ALL DECODE CHECKS PASSED — 48/48".
- route sweep: `api_route_sweep.py` -> `/v1/cards 200`; `/v1/cards/{id}` hit directly via curl -> 200.

## Deliberately NOT fixed
- Stale/offline last-known fallback on this view: it does not use `LoadState`.
  Converting it would be consistent with ProjectBoardView, but the single-resource
  detail screen is lower-traffic and the honest error pane is acceptable. Larger
  consistency refactor; tracked as reasoning, not executed fix.
- Pull-to-refresh on the detail view: minor UX nicety, the board above it has it.
- Showing any of the 8 structural fields I did not add (branch/priority/timestamps/
  workspace): low operator value on a detail screen vs clutter. Reported as
  available but deliberately dropped.

## Changed files (this worktree, branch wt/t_cb300f9c)
- ios-app/Sources/HSCC/Models.swift
- ios-app/Sources/HSCC/Views/CardsView.swift
- ios-app/scripts/live_decode_check/main.swift
- ios-app/scripts/live_captures/20260902_222848/  (captured live evidence)
