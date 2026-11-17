kind: Changed
task: t_09cd8abf

- **The board status widget now has a real settings surface — Active toggle and
  cadence — in the app's "Widgets & Live Activities" screen
  (`ios-app/Sources/Shared/SharedStore.swift`, `Sources/HSCC/Views/ConfigView.swift`,
  `Sources/HSCCWidgets/BoardStatusWidget.swift`).** The widget shipped (PR #319)
  with a hard-coded quarter-hour cadence and no way to pause it, because
  `WidgetSurfaceKind` had no case for the surface and adding one means the shared
  store plus the config screen — a second file set. It now has one:
  `case widgetBoardStatus = "widget.boardStatus"`, read by
  `BoardStatusTimelineProvider` exactly the way `ClusterWidgetTimelineProvider`
  reads `.widgetCluster`. Two behaviours, both the shipped precedent's: an
  **Off** cadence (0) yields `policy: .atEnd` rather than scheduling an instant
  refresh loop, and a **paused** surface returns a `paused` entry *before* the
  client is built — zero requests, no live counts, no last-known snapshot, no
  failure pane — drawing "Paused in Settings" in every family (a dash and the
  label `hscc · paused` where there is no room for a pane). A paused widget that
  kept drawing its last-known numbers, or went blank, is the failure
  `ClusterEntry.paused` exists to prevent.
  **The default is ON at the shared widget cadence (5 min), not the parent's 15.**
  Deliberately: `AppGroupWidgetSettings.setting(for:)` fills an absent surface
  with the widget defaults, so a device that never opened the settings screen
  keeps rendering, and adding a registry key cannot silently pause a widget that
  is already on someone's Home Screen.
  **`availableSections` for this surface is the empty set**, and that is a
  decision, not an omission: the widget draws one dataset — five counts for one
  board — and nothing else, so any section toggle here would either empty the
  widget or do nothing, and a toggle that does nothing is a lie in the settings
  UI. `WidgetSurfaceEditor` therefore hides its whole "Show Content" group when a
  surface declares no sections, so the screen offers no dead choice. (The
  cluster and monitor surfaces keep their toggles; they consume them.)
  The config screen needed no new row logic — its rows are already
  `ForEach(WidgetSurfaceKind.allCases)` over the shared `WidgetSurfaceEditor`;
  this adds the display metadata (name, and the same SF Symbol the widget draws
  in its own header so the row and the widget read as one thing) plus the empty
  section guard.
  Two gates changed with it, both because they transcribed a fact instead of
  reading it: `widget_settings_check.sh` asserted `defaults.surfaces.count == 4`,
  so a fifth surface reddened the gate, and now reads the count off
  `WidgetSurfaceKind.allCases` — plus executed cases for the new key (pause and
  Off survive the encode/decode the extension performs; an install that has never
  seen the key still defaults ON at 5 min). `widget_view_dispatch_check.sh`
  covered only the cluster widget's dispatch; it now also proves this widget's
  five live-data bodies gate on `!configured || paused`, that the paused decision
  lands *inside* `entry(for:surface:)` before the client exists (so "a paused
  surface sends zero requests" is measured, not claimed), and that the cadence
  rule holds — by slicing `nextRefreshDate` out of the committed source by
  `GATE-SLICE` marker and executing it on the macOS SDK, so the Off branch (the
  one that becomes a refresh loop if it is wrong) is a run assertion and not a
  comment. `project.yml` is untouched: no file added or deleted. Nothing was
  built or run on a device or simulator.
