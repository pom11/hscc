kind: Added
task: t_f9cc34e0

- **`ios-app/Sources/HSCCLiveActivityMonitor/CardsRunningLiveActivity.swift` —
  the running-cards Live Activity: Dynamic Island + Lock Screen.** The phone
  form of `hscc cards monitor --live`'s panel header — board · card · worker ·
  run time — for the whole fleet at once. Dynamic Island: the cards glyph
  leading, the count trailing (`!` for a read that produced nothing, `—` for a
  subset with no server count), the glyph alone minimized, and the expanded
  region with headline + a `stale · <why>` line + the pushed age + up to three
  rows; Lock Screen: the same header plus the row list, longest-running first.
  The content state carries the same five fields the running-cards *widget*
  stores (board slug, card id, title, worker, status, minutes) and nothing
  else — no card prose, no workspace path, no branch, no address, nothing that
  could carry a secret into an extension process.
  Read-only by construction: a Live Activity is push-only, so this file renders
  what the app pushes and never fetches, and it draws no column for anything
  the kanban routes do not send.
  `Sources/Shared/CardsRunningActivity.swift` holds the attributes and, beside
  them, the two pure rule blocks the rendering forwards to:
  `CardsRunningReading` (headline / count / blocked / age) and
  `CardsRunningOrphanRule` (the launch-sweep decision). Those rules own one
  promise the whole pivot rests on — **a blind read never renders as an idle
  board**: a read that answered nothing leads with its failure label and can
  produce no `0 running`, and rows that are a subset with no server `count`
  render `—` rather than their own number.
  `Sources/HSCC/CardsRunningActivitySweep.swift` runs the matching re-hydration
  sweep at `didFinishLaunching`, beside the monitor's, so the bubble a
  force-quit strands is ended on the next launch instead of ageing forever.
  Deliberately NOT in this slice: the poll-and-push driver that starts the
  activity. It needs a `WidgetSurfaceKind` case to honour the shared Active
  toggle and cadence, and that means `SharedStore.swift` plus the widget/Live
  Activity config screen — another card's file set. Borrowing the cluster
  monitor's toggle would let switching off one surface silently stop the
  other, so the surface ships renderable and swept, and unfed.

---
kind: Changed
task: t_f9cc34e0

- **`ios-app/Sources/HSCCLiveActivityMonitor/` — the monitor extension now
  hosts two Live Activities, and the "one per target" rule is retired.** The
  extension's `@main` is `HSCCLiveActivityMonitorBundle`, a `WidgetBundle`
  declaring one `ActivityConfiguration` per attributes type;
  `MonitorLiveActivity.swift` loses its `@main` with it. Three files in this
  repo (this file's header, `project.yml`, `README.md`) asserted that a widget
  extension body can contain only ONE `ActivityConfiguration`, which is why
  wake/session/monitor were three targets. That is false on the iOS 26 SDK —
  the bundle above compiles clean (0 errors, 0 warnings) under
  `build_check.sh`'s own invocation — so activities added to the monitor family
  now join this target instead of costing an extension, an entitlements file, a
  bundle id and an embed dependency each. The wake activity keeps its own
  target; nothing here undoes that split.

---
kind: Verified
task: t_f9cc34e0

- **`ios-app/scripts/live_activity_rehydration_check.sh` — extended to the
  running-cards activity, by slicing rather than transcribing.** 25 new
  assertions over the orphan rule (rows + fresh push keep; rows past the
  silence budget end; **zero rows end at any age**, including a fresh one and
  one stamped in the future — an empty bubble is never "still in flight") and
  over the reading rules (server count beats the rows; a subset with no count
  yields `nil`; a blind headline contains no zero; `99+` clipping; `— blocked`
  distinct from `0 blocked`; a clock ahead of the device reads `now`).
  `live_activity_rehydration_check/main.swift` copies none of it: the script
  extracts the two `GATE-SLICE` blocks out of `CardsRunningActivity.swift` and
  compiles them with the harness, so the table runs against the code the app
  and the extension actually build, and a moved marker fails the script **by
  name** instead of quietly shrinking the table. Proven red on purpose by
  renaming one marker (`FAIL: … has no 'GATE-SLICE-BEGIN:
  cards-orphan-decision' marker`, rc 1) and restoring it.
