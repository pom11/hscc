kind: Added
task: t_71deac83

- **`ios-app/Sources/HSCC/CardsRunningActivityDriver.swift` — the running-cards
  Live Activity is now fed.** t_f9cc34e0 shipped the rendering and the sweep,
  and nothing called `Activity.request` for
  `CardsRunningActivityAttributes`, so the surface was gate-proven but unfed.
  One refresh reads the same pair the widget reads — `/v1/kanban/running` plus
  `/v1/kanban/blocked`, never `/v1/cards` — through `WidgetData.read`, with the
  widget's own stored blob as `lastKnown:`, and pushes a content state. Its two
  honesty rules live in the shipped source where a gate can reach them: a read
  that did not answer **never** yields `runningCount = 0` (the count comes from
  `CardsRunningProjection`, which exists only for a read that did answer, so a
  blind refresh pushes `nil` — drawn `—` — beside a `failureLabel` naming the
  outage), and a refresh that found nothing running **ends** the activity
  instead of leaving an empty bubble up. It reaches that second conclusion
  through the same shape as `CardsRunningOrphanRule`, so the app never pushes a
  bubble the next launch immediately kills.
  A push adopts a leftover activity rather than requesting a second bubble for
  the same fleet. Cadence and the pause follow the shared settings surface this
  card adds — `WidgetSurfaceKind.liveActivityCards`, its own row in *Widgets &
  Live Activities*, its own Active toggle and cadence, never borrowed from the
  cluster-health monitor's — with `Off` (0 min) meaning no loop and no
  background task, and `isActive == false` checked before a client is built so
  a paused surface sends zero requests. Background refresh rides a
  `BGAppRefreshTask` under a second identifier,
  `com.hscc.ios.cards.backgroundRefresh`, because one handler can only refresh
  one surface and sharing the monitor's would make cadence one knob.
  `CardsRunningFeed.read` — the two-route pairing, the two-request budget and
  the guard against a `tasks`-less 2xx — moved into
  `ios-app/Sources/Shared/CardsRunningProjection.swift`, which both targets
  compile: the widget provider delegates to it and the driver calls it, so
  there is one reading rule rather than a copy that can drift.

---

kind: Verified
task: t_71deac83

- **The driver's decisions are gate-asserted, and the settings screen's
  delegation is no longer only prose.** `live_activity_rehydration_check.sh`
  gained a third `GATE-SLICE` block, `cards-feed-decision`, holding the driver's
  push/end/leave-alone rule in Foundation-only code the script slices out of
  `Sources/Shared/CardsRunningActivity.swift` and executes — the existing two
  blocks are untouched — plus a part that compiles
  `CardsRunningProjection.swift` as shipped and asserts the count honesty there:
  the answered-empty-list real zero, the server's count beating a subset of
  rows, a down blocked route giving `nil` and never `0`, unknown age sorting
  last, the stored row cap, an unparseable `started_at` giving no age.
  `widget_settings_check.sh` gained two content guards (a content group is not
  offered for a surface with no sections; the scope pickers appear only where
  the route honours a scope) and two fixes to assertions that were already
  misjudging the code: the *all cases are editable* check required
  `sectionRow(kind:)` for a surface with no sections and never for one with any,
  which contradicts the shipped board-monitor/cluster-health rows, and the
  surface count was transcribed as `== 4` while the model had grown a fifth
  case — a literal that reddened the next legitimate surface and would have
  invited someone to delete a real one. The count is now derived from the code
  the check already reads.
  Negative tests, each reverted and verified byte-identical after: a suffixed
  marker fails the rehydration script by name; letting a blind read start a
  bubble, letting a non-live push carry no label, and letting a down blocked
  route answer `0` each fail a named assertion; replacing either config-screen
  guard with `if true` fails its named guard; and copying the five-surface model
  onto the pre-fix script fails `defaults has all 4 surfaces` — the trap the fix
  removes. Full gate at the head, every `scripts/*check*.sh` run side by side
  with a clean checkout of the base: no script changed colour.
