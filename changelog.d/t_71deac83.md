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
  the route honours a scope) and the registry-invariant cases: no two surfaces
  share a registry key, every key is namespaced by surface kind, a surface with
  no sections stays section-less through the bounded read, and the running-cards
  surface is push-fed under a stable key. Each reads the registry's own
  `allCases` rather than a remembered number, so the next surface cannot redden
  a healthy gate.

- **Two edits this branch first brought are deliberately NOT in the delivered
  diff, because `main` already carries the same rule in a better form** (rebase
  onto `main` @ `e550f944`). The surface-count assertion is already a set
  comparison over `allCases`, and the backtick splice this branch patched by
  routing the extracted model block through `printf` inside quoted heredocs is
  settled file-wide by the same card's ban plus the committed harness golden —
  keeping the branch's narrower shape would have put a second mechanism in place
  of one and frozen the harness body out of the escape the ban documents. That
  the ban is the sufficient half is measurable rather than argued: the committed
  golden carries backticked lines lifted verbatim out of
  `Sources/Shared/SharedStore.swift`, intact, so a backtick arriving through an
  expansion is inert text and only the ones typed into this gate's own prose can
  splice.

- **One pre-existing red on `main` is repaired by this branch.** At `e550f944`
  `widget_settings_check.sh` failed its OWN backtick guard — comment lines in the
  harness body had landed carrying literal backticks after the guard shipped — so
  it exited before compiling anything; and its committed harness reference had
  never been refreshed for the model block `main` grew since, so no current model
  matches those bytes. Both are fixed here: the prose interpolates the documented
  escape instead of carrying the byte, and the reference is refreshed for the
  current model plus the cases this branch adds. Measured by running the same
  script set in two clean worktrees — the base and this head: the base fails
  exactly this one gate and passes every other script in the set, and this head
  passes the whole set.
