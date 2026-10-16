# Nav-graph walker gate (t_810ab152) — design notes

Status: IN PROGRESS (skeleton committed first, findings appended as they land).

## What this is

Operator brief v2 (kanban comment 2026-10-15): the gate is **XCUITest** (real
taps, no human) — NOT the simctl nav-dump walker the card body described
(mechanism superseded; the assertion list still applies verbatim). The
DrawerNavigationUITests target from t_5ed5d281 exists and is extended here.

Files owned by this card:
- `ios-app/scripts/nav_graph_screens.py` — code-derived screen enumerator
  (parses AppShell.swift + Sidebar.swift, emits the walk table).
- `ios-app/UITests/NavGraphScreenTable.swift` — GENERATED (committed; the
  gate fails on drift so a new screen cannot dodge the walk).
- `ios-app/UITests/NavGraphUITests.swift` — the walker.
- `ios-app/scripts/nav_graph_check.sh` — the gate (seed fixtures incl.
  empty-mode set, run suite green, run guarded negative test red).
- `ios-app/Sources/HSCC/DebugHarness.swift` — DEBUG seams (fixture dir
  override, failure mode, offline mode, quiet-poller mode, live
  served-count probe feed `ptrProbeFeed`).
- `ios-app/Sources/HSCC/Views/AppShell.swift` — NEGATE_BACK seam,
  quiet-poller gate on the badge pollers, and the `ptrProbe` AX text.
- `ios-app/scripts/seed_fixtures.sh` + `scripts/make_empty_fixtures.py` —
  push the main + zero-row fixture sets into the app container (shared by
  the gate and the dev loop; the gate seeds, these scripts build the sets).

## Code-derived enumeration (no hardcoded screen list)

`nav_graph_screens.py` parses, from the Swift sources:
- every `AppDestination` case + its `title` (AppShell.swift);
- every drawer row `row(.x` (Sidebar.swift);
- every legacy alias `case .x: return .y` (AppShell.normalized);
- every `ProjectRoute` push in AppShell.projectRouteView.
Rule enforced by the generated table + the walker: every destination must be
either a drawer row or an alias that maps to one; every drawer row must carry
a curated walk expectation (title marker, empty marker, error marker, PTR
feed path). A new screen without an expectation generates
`expectation: nil` and the suite fails naming the screen. `nav_graph_check.sh`
re-runs the parser and fails on any drift against the committed table.

## Assertions (operator list, verbatim)

Per drawer row: enter via drawer -> title marker on screen -> back to drawer
-> tap the SAME row again (2.9.0 dead-end regression) -> next row.
One level in where detail exists: projects -> project -> chat; cards ->
card detail; each back returns to its predecessor — PROVEN BY LANDING
(the marker of the predecessor screen must render after the pop), not by
asserting the back button's label: a screen pushed via
`router.projectsPath.append` on top of a value-less NavigationLink push
inherits the ROOT screen's title as its back label (chat under a project
overview reads "Projects", AX-verified on iPhone 16 / iOS 26.3), so the
label is observation-only while the landing is the contract.
Pull-to-refresh: ONE slow XCUICoordinate drag (`press(forDuration: 1.0,
thenDragTo:)`). PROVEN 2026-10-16 on iPhone 16 / iOS 26.3: `swipeDown()` on
any element NEVER fires SwiftUI `.refreshable` (served-count probe stayed at
1 through every element-swipe variant); the coordinate drag increments it.
Receipt = the app's live served-count AX probe (`ptrProbe`, published by
DebugHarness for the walked feed) — NOT the Documents manifest: the runner
cannot see app files (proven the same day; that is why the receipt moved
on-screen). Badge pollers (ApprovalPoller, StreamReplyWatcher,
NotificationCoordinator) are silenced for walked launches
(`HSCC_DEBUG_QUIET_POLLERS=1`) so the increment can only be the PTR.
Empty: `HSCC_DEBUG_FIXTURE_DIR=empty` (zero-row fixture set) -> honest empty
marker renders AND drawerOpen still there.
Error: `HSCC_DEBUG_FIXTURE_FAIL=1` (all GETs answer 500) -> failed-row marker
renders AND the screen is still navigable.
Disconnected pass: no fixture env at all -> every row still round-trips the
drawer and shows its honest gate/offline row.

## What the walker proves / does not prove

- `capture_screens.sh` proves a screen RENDERS (fixture-fed, manifest-checked).
- The walker proves every reachable screen is USABLE the way a user walks it:
  enter, back, PTR re-request, honest empty + error states, no dead ends.
- Neither proves PIXEL correctness (layout, colour values, font scale) —
  that stays with check_theme.sh + human/design review.

## Negative self-check

`HSCC_DEBUG_NEGATE_BACK=1` (DEBUG-only AppShell seam) removes the drawer
open/close affordances; `nav_graph_check.sh` runs the walk once under it and
REQUIRES red output naming a screen — a walker that cannot fail is not a gate.

## Findings log

(appended as discovered; every dead-end/trap fix gets its own commit)
