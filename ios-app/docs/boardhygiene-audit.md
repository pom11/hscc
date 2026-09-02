# Screen audit: BoardHygieneView — prove every element works

Task: t_c13011d7  ·  Assignee: ios-engineer  ·  Date: 2026-09-02
View audited: `ios-app/Sources/HSCC/Views/BoardHygieneView.swift`
Branch: `audit/boardhygiene-t_c13011d7`

## Methodology
* All claims backed by executed commands in a scratch worktree; none by guesswork.
* No iOS runtime here — iOS findings are compile + source + decode + logic
  proof (executed), stated plainly where pure reasoning.
* Live API referenced read-only; address derived at runtime, never hardcoded.

---

## VERDICT (summary)

**BoardHygieneView is sound.** Both data feeds answer with real, complete data;
every field the view renders arrives; the three on-screen states (empty / error /
stale) are clearly distinguishable; the Recover control is confirm-gated, its
route is registered server-side, and it reloads + surfaces the real message. No
"dead-end no-op" path exists here (the failure mode the operator has been
burned by). Compile is clean across all 4 targets, 0 warnings.

No clearly-broken bug found → **no code changes made.** Three minor findings
reported and ranked below, none of which warrants a change this pass.

---

## 1. DATA IN — which endpoints, and does every field arrive?

Two GET routes feed this view (plus one POST for the Recover control):

| Route | Client method | Evidence |
|---|---|---|
| `GET /v1/kanban/blocked` | `client.kanbanBlocked()` (HSCCClient.swift:648) | 200 OK, decodes POPULATED |
| `GET /v1/kanban/stale?older_than=0` | `client.kanbanStale(olderThan:)` (HSCCClient.swift:653) | 200 OK, decodes POPULATED |
| `POST /v1/kanban/blocked/{id}/recover` | `client.recoverBlockedCard()` (HSCCClient.swift:878) | route registered (routes_kanban.py:312) |

**Live blocked capture** (`scripts/live_captures/20260902_224418/v1_kanban_blocked.json`):
```
boards: 15 (int), count: 7, errors: [], speak: "7 cards blocked across 15 boards."
tasks: 7 cards, keys = [board, id, status, assignee, age_days, block_kind,
                        why, title, comments]
```
Every field the blocked pane renders is present: `displayTitle`←(title/id),
`board`, `assignee`, `age_days`, `block_kind`, `why`. ✓

**Live stale capture** (`.../v1_kanban_stale.json`):
```
boards: [15 name strings], count: 21, errors: [], older_than: 0,
speak: "21 stale cards."
tasks: 21 cards, keys = [board, id, status, assignee, age_days, title]
```
Every field the stale pane renders is present: `displayTitle`←(title/id),
`board`, `status`, `assignee`, `age_days`. ✓

**Proof the real models decode the real live bodies:**
```
scripts/live_decode_check.sh scripts/live_captures/20260902_224418
  DECODE+  v1_kanban_blocked.json  →  KanbanBlockedResponse  [POPULATED]
  DECODE+  v1_kanban_stale.json    →  KanbanStaleResponse    [POPULATED]
  LIVE DECODE: 33/33 decoded, 33/33 populated
```
Models: `KanbanBlockedResponse` (Models.swift:809), `KanbanStaleResponse`
(Models.swift:844), `BlockedCard` (Models.swift:779), `StaleCard` (Models.swift:833).

**No field the view needs is missing.**

---

## 2. RENDER — what does the operator see?

* **Blocked pane** (`blockedList`, BoardHygieneView.swift:85): a `speak` summary
  Label ("7 cards blocked across 15 boards."), then one row per card showing
  display title, a meta line (board · assignee · N d), an amber `hand.raised`
  chip for the block kind when present, the `why` text, and a `Recover` button.
* **Stale pane** (`staleList`, BoardHygieneView.swift:173): `speak` Label ("21
  stale cards."), then one row per card: title + meta line (board · status ·
  assignee · N d old).

**Client-side vs server count:** The view renders `ForEach(tasks)`. Live: blocked
`count=7` = 7 task rows; stale `count=21` = 21 task rows. **No client-computed
count is displayed at all** — the only numbers the operator sees are the
server's own `speak` and the per-card `age_days`. So there is no surface where a
client-computed count could disagree with the server. ✓

**Units:** `age_days` rendered with explicit units: blocked `"\($0)d"` → "0d",
stale `"\($0)d old"` → "0d old". Consistent, unambiguous (days, not seconds/hours).
✓

**Dropped fields:** the `boards` envelope field is not rendered by the view. For
blocked it's an `Int` and already folded into `speak` ("across 15 boards"); for
stale it's a `[String]` board-name list that the view intentionally doesn't show
(that's plausible — the task list already shows each card's board in the meta
line). Not a bug; the board names are redundant with the per-row `board` field.

**Truncation:** `Text(...)` rows are in a vertical `VStack` inside a `List`
(`.frame(maxWidth:.infinity, alignment:.leading)`) so titles/why wrap rather
than truncate. The only horizontal clamp is `HSMetaLine`'s `HStack` of captions —
on a very narrow screen (SE) a long board name + assignee + age in one line can
get cramped, but each part is short and it wraps acceptably. Minor.

---

## 3. STATES — loading / empty / error / stale are DISTINCT

The view uses the shared `LoadState` surface via `Offline.load`
(LoadState.swift) — the same honest-state machinery audited in prior screens.

| State | When | What the operator sees |
|---|---|---|
| **Loading** | first fetch | Full-pane `HSLoading("Loading…")` spinner (BoardHygieneView.swift:69, 157) |
| **Empty (success, 0 rows)** | 200 with `tasks=[]` | `List` with `speak` Label + muted text "No blocked cards on any board." (blocked :101) / "No stale cards." (stale :189). Server's own `speak` ("0 …") shown too. |
| **Error (never fetched)** | fetch failed, no cached/in-session value | `HSError("Couldn't load blocked cards", message:…)` — ContentUnavailableView, red `exclamationmark.triangle`, real reason, **"Try again" button** (:73, :161) |
| **Stale/offline (has last-known)** | fetch failed, cached value exists | `StaleBanner` on top: "Offline — showing state from X ago" + "Can't reach the cluster right now." + retry arrow, then the last-known list (:89, :177) |

**Empty ≠ error.** "No blocked cards on any board." (muted single line, inside a
List) is visually and semantically distinct from `HSError` (full-pane red
triangle + Try again). ✓ Stale is distinct from both (amber Offline banner +
data). ✓ The rule "0 results and failed-to-load must never look the same" is met.

**Stale/offline:** handled via `Offline.load` with `StaleBanner`. Confirmed
working path (same as FleetControlView audit, which fixed exactly this class of
gap — this screen already has it).

---

## 4. CONTROLS — every one traced, route answers, feedback present

The user-visible controls on this screen:

1. **Segmented pane picker** (SwiftUI `Picker(.segmented)`,
   BoardHygieneView.swift:40). Selects Blocked/Stale. Immediate, local, has
   visual feedback (selection highlight). ✓ No network.
2. **Pull-to-refresh** (`.refreshable` on both Lists, :116 / :210) → re-runs
   `loadBlocked` / `loadStale`. Routes answer (see §1). Feedback: the list
   reloads; the built-in refresh spinner. ✓
3. **StaleBanner retry** (Theme.swift:403) → re-runs the same load. Feedback:
   content updates. ✓
4. **HSError "Try again"** (:73 / :161) → re-runs load. ✓
5. **Recover button** (MutationButton, BoardHygieneView.swift:134) — the only
   MUTATION on this screen. Traced end-to-end:
   * Tap → arms `confirmationDialog` only (MutationSupport.swift:92) — a single
     tap can NEVER fire a mutating request. ✓
   * Confirm → `run()` → `client.recoverBlockedCard(card.id)` → **always sends
     `confirm: true`** (HSCCClient.swift:880) → `POST
     /v1/kanban/blocked/{id}/recover`.
   * Server route registered (routes_kanban.py:312) → `handle_kanban_recover`
     (routes_kanban.py:166): require-confirm (409 without), 404 if not blocked,
     502 on failure, else 200 with `{id, board, reason, message, speak}`.
   * Recovery then re-fetches: `await loadBlocked(client)` inside `run` — so by
     the time the success alert appears the card is already gone from the list.
   * **Feedback:** `MutationButton` shows a spinner while in-flight (disabled,
     no double-fire) and always surfaces the outcome via alert — success shows
     the real server `message` ("recovered t_x (board 'hscc') to ready"); any
     failure (404/502/409) throws → red "Failed" alert with the real message.
     **Never a blank success** (MutationSupport.swift:124-132). ✓

**Route coverage cross-check** (`scripts/api_route_sweep.py`, executed):
```
  ok   200  /v1/kanban/blocked
  ok   200  /v1/kanban/stale
13 interpolated route(s) not swept (need a live id):
    /v1/kanban/blocked/\(encoded)/recover   ← POST, confirm-gated, NOT swept by design
All swept routes answered with parseable JSON.
```
The recover route is intentionally not fired by the sweep (mutating); verified
registered in source (routes_kanban.py:312) and its handler validates `confirm`
+ returns the exact shape `RecoverCardResponse` (Models.swift:820) decodes.
**Every control on this screen has visible feedback; none dead-ends.**

---

## 5. OBSERVATION — every ObservableObject held correctly

**This view holds ZERO ObservableObjects.** All its state is value-typed:
```swift
@State private var selected: Pane = .blocked
@State private var blocked = LoadState<KanbanBlockedResponse>.idle
@State private var stale  = LoadState<KanbanStaleResponse>.idle
```
(BoardHygieneView.swift:29-31). There is no `let`-held ObservableObject, so the
"plain `let` won't re-render" (switch-tabs) bug **cannot occur here** — SwiftUI
re-renders automatically when any `@State` changes. ✓

**@StateObject keyed by a changing value?** There is no `@StateObject` at all.
✓

**Pane-switch state retention:** `@State` lives on the struct instance, which is
pushed once onto the `NavigationStack` (ClusterView.swift:210 via NavigationLink
at :287) and persists for the screen's lifetime. Switching the segmented picker
does NOT recreate the struct, so a loaded Blocked pane stays loaded when you peek
at Stale and come back — **no reload churn, no "had to switch tabs"**. Leaving
the screen re-creates the struct fresh (idle → reload), which is acceptable and
matches every other audited hub screen.

---

## 6. LAYOUT — Dynamic Type + small screen

* Rows are `VStack(alignment:.leading)` inside List rows with
  `.frame(maxWidth:.infinity, alignment:.leading)` — titles, `why`, and the
  chip all wrap instead of truncating. ✓ Dynamic-Type-safe (system fonts `.body`
  / `.caption` scale).
* `Age`/meta use caption text + `.sm` separators; on iPhone-SE width a long
  board name + assignee + age on one `HSMetaLine` HStack can get snug but
  shortens/wraps acceptably (only real layout risk on this screen; low impact).
* Segmented picker has `.horizontal` padding, fits SE width. ✓
* `Recover` is a `.borderless` `MutationButton` (caption font) on its own line —
  no crowding. ✓

**Not a bug**: the screen is a `NavigationStack`-wrapped `VStack` in a `List`-less
container; the inner `List` punches to full height fine; the segmented control +
list stack read cleanly on SE.

---

## 7. ACCESSIBILITY — icon-only / colour-only signals

* **Segmented Picker** has a label ("Pane"). ✓
* **Recover** is a text+icon button ("Recover" + arrow.counterclockwise) — not
  icon-only. ✓
* **Block kind chip** uses colour (warn) AND text + `hand.raised.fill` icon — not
  colour-only. ✓
* **Empty text** uses muted colour but the words "No blocked cards on any board."
  are present — not colour-only. ✓
* **Error state** red triangle + "Couldn't load blocked cards" + "Try again" —
  text present. ✓
* **StaleBanner retry** has `.accessibilityLabel("Retry loading")` (Theme.swift:434).
  ✓
* **Meta/caption lines** are secondary info, fine as-is.

No icon-only control and no colour-only signal on this screen. ✓

---

## Findings (ranked by how likely the operator is to hit them)

**F1 — [minor] Stale pane has no DURABLE offline cache (blocked does).**
`loadStale` (BoardHygieneView.swift:221) calls `kanbanStale(olderThan: 0)`, which
routes through `HSCCClient.get(path:queryItems:as:)` (HSCCClient.swift:240).
That overload only writes `StateCache` when `queryItems.isEmpty` (HSCCClient.swift:260)
— `older_than=0` makes it non-empty, so the stale response is **never persisted**.
`Offline.load`'s first fallback (`client.cachedValue`) therefore always misses for
stale; only the in-session `current.value` fallback works. Compare blocked
(`get(_:as:)`, HSCCClient.swift:198) which always caches → durable offline
last-known. Impact: if the operator is offline AND the app was relaunched since
the last load, the Stale pane shows a hard error instead of last-known data.
They'd have to (a) already have loaded stale successfully, (b) go offline, (c)
terminate & relaunch the app, (d) open the Stale pane. Low likelihood. The path
for `hscc kanban stale` includes `older_than=0` by design (comment on :225), so
the right fix would be a targeted cache in the client for this specific read —
not worth the shared-client risk this pass. **Not fixed** (see below).

**F2 — [very low] `boards` envelope field unused.** Blocked `boards:Int` and
stale `boards:[String]` are decoded but never rendered. Harmless — the info is
redundant with `speak`/per-row `board`. Deliberately not surfaced.

**F3 — [very low] Meta line can get snug at SE width.** 3-part `HSMetaLine`
(board · assignee · age) with separators may crowd on iPhone SE. Acceptable;
already the app-wide shared pattern (Theme.swift:377). Not a defect.

---

## What I fixed

**Nothing.** The screen is correct; no clearly-broken bug found. The strongest
evidence (live decode POPULATED, route sweep 200, registered recover route,
distinct empty/error/stale rendering, no ObservableObject to hold wrong)
supports "no issues found WITH evidence" — which the task explicitly frames as a
valuable result.

## What I deliberately did NOT fix and why

**F1 (stale offline cache)** — the one real asymmetry. Not fixed because:
1. It is a graceful-degradation nicety, not a dead-end or data-corruption bug.
2. The correct fix lives in the shared `HSCCClient` cache policy, touching the
   paging-read semantics that `get(path:queryItems:)` was built to protect —
   risk of regressing other screens outweighs the marginal benefit.
3. The operator must satisfy 4 conditions (offline + relaunch + stale pane +
   previously loaded) to even observe it, and when it happens the failure is
   honest (real error message), not silent.

If the operator wants durable stale offline state, spawn a follow-up task to
make the client cache the `older_than=0` stale read specifically (e.g. treat
`older_than=0` as an "all stale" read worth caching, mirroring how the tail-page
read is privileged).

---

## Proof trail (commands that ran)
* `bash scripts/build_check.sh` → HSCC 57 files 0e/0w; all 4 targets clean.
* `bash scripts/check_sources.sh` → 62 files, all in project.yml.
* `bash scripts/check_theme.sh` → CLEAN, no raw colour outside Theme.swift.
* `bash scripts/capture_live.sh` → 33 routes captured.
* `bash scripts/live_decode_check.sh …/20260902_224418` → 33/33 POPULATED
  (blocked + stale both POPULATED).
* `python3 scripts/api_route_sweep.py` → blocked 200, stale 200, all parseable;
  recover listed as interpolated/POST (not swept by design).
* `grep` over `hscc-api/routes_kanban.py` → recover route + handler +
  confirm-gate + response shape verified in source.
