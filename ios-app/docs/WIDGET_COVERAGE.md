# Widget coverage audit — iOS and macOS

Measured on the merged tree rebased on `origin/main` (2026-11-18), after the
cluster route parity fix and the CARD B3 board/project refurbish. A family is
counted as covered only when it is both listed in that widget's
`supportedFamilies` **and** has a non-placeholder view in the family dispatch
switch (or routes through the shared, gated text model the dispatch selects).
The Cluster and Monitor surfaces were refurbished by the sibling card; the
Kanban Monitor, Board Status and Projects widgets plus the Board Progress Live
Activity were refurbished by CARD B3. Every surface reads the fields in
`hscc-api/docs/WIDGET_ROUTES.md`.

Legend:

- **declared-and-renders** — in `supportedFamilies`, and the dispatch switch
  names the view that draws it.
- **declared-but-placeholder** — in `supportedFamilies`, but the family only
  draws `placeholder(in:)` / sample data.
- **absent** — not in `supportedFamilies` for that platform.
- **unknown** — could not be determined from the code; the reason is stated.

No cell below is `declared-but-placeholder` or `unknown`.

## 1. iOS widgets × family

Every widget type under `ios-app/Sources/HSCCWidgets/` is listed. The bundle at
`HSCCWidgetsBundle.swift` contains exactly these six widgets; there are no
additional `Widget` types in that directory.

| Widget | systemSmall | systemMedium | systemLarge | accessoryInline | accessoryCircular | accessoryRectangular | Source |
|---|---|---|---|---|---|---|---|
| Projects | declared-and-renders (`SmallProjectsWidget`) | declared-and-renders (`MediumProjectsWidget`) | declared-and-renders (`LargeProjectsWidget`) | declared-and-renders (`Text(entry.inlineLine)`) | declared-and-renders (`ProjectsCircularView`) | declared-and-renders (`ProjectsRectangularView`) | `ios-app/Sources/HSCCWidgets/ProjectsWidget.swift` |
| CardsRunning | declared-and-renders (`CardsRunningSmallView`) | declared-and-renders (`CardsRunningMediumView`) | declared-and-renders (`CardsRunningLargeView`) | declared-and-renders (`Text(entry.inlineLine)`) | declared-and-renders (`CardsRunningCircularView`) | declared-and-renders (`CardsRunningRectangularView`) | `ios-app/Sources/HSCCWidgets/CardsRunningWidget.swift` |
| KanbanMonitor | declared-and-renders (`SmallKanbanMonitor`) | declared-and-renders (`MediumKanbanMonitor`) | declared-and-renders (`LargeKanbanMonitor`) | declared-and-renders (`KanbanMonitorInline`) | declared-and-renders (`KanbanMonitorCircular`) | declared-and-renders (`KanbanMonitorRectangular`) | `ios-app/Sources/HSCCWidgets/KanbanMonitorWidget.swift` |
| BoardStatus | declared-and-renders (`BoardStatusSmallView`) | declared-and-renders (`BoardStatusMediumView`) | declared-and-renders (`BoardStatusLargeView`) | declared-and-renders (`Text(entry.inlineLine)`) | declared-and-renders (`BoardStatusCircularView`) | declared-and-renders (`BoardStatusRectangularView`) | `ios-app/Sources/HSCCWidgets/BoardStatusWidget.swift` |
| Cluster | declared-and-renders (`SmallClusterWidget`) | declared-and-renders (`MediumClusterWidget`) | declared-and-renders (`LargeClusterWidget`) | declared-and-renders (`InlineClusterWidget`) | declared-and-renders (`CircularClusterWidget`) | declared-and-renders (`RectangularClusterWidget`) | `ios-app/Sources/HSCCWidgets/ClusterWidgetViews.swift` |
| Monitor | declared-and-renders (`SmallMonitorWidget`) | declared-and-renders (`MediumMonitorWidget`) | declared-and-renders (`LargeMonitorWidget`) | declared-and-renders (`InlineMonitorWidget`) | declared-and-renders (`CircularMonitorWidget`) | declared-and-renders (`RectangularMonitorWidget`) | `ios-app/Sources/HSCCWidgets/MonitorViews.swift` |

Coverage note: all six iOS widgets also contain a `.systemExtraLarge` branch in
their view switch, but none declares `.systemExtraLarge` in `supportedFamilies`
on iOS. That family is therefore **absent** on iOS by configuration, not a
missing renderer.

## 2. Live Activities × surface

All five `ActivityAttributes` types declared under `ios-app/Sources/Shared/` are
on `main`. Live Activities are push-only: the extension renders, and an app-side
driver performs the fetches and calls `update()`. The monitor extension never
refreshes an activity itself.

| ActivityAttributes | Lock screen | DI compact | DI expanded | DI minimal | Hosted by `HSCCLiveActivityMonitor`? | Refresh owner |
|---|---|---|---|---|---|---|
| `MonitorActivityAttributes` | declared-and-renders (`LockScreenMonitorView`) | declared-and-renders (`compactLeading` / `compactTrailing`) | declared-and-renders (`DynamicIslandExpandedRegion` leading/center/bottom) | declared-and-renders (`minimal`) | Yes | No in the extension: `MonitorActivityDriver` polls and pushes |
| `CardsRunningActivityAttributes` | declared-and-renders (`LockScreenCardsView`) | declared-and-renders (`compactLeading` / `compactTrailing`) | declared-and-renders (`DynamicIslandExpandedRegion` leading/center/trailing/bottom) | declared-and-renders (`minimal`) | Yes | No in the extension: `CardsRunningActivityDriver` polls and pushes |
| `ClusterHealthActivityAttributes` | declared-and-renders (`LockScreenClusterHealthView`) | declared-and-renders (`compactLeading` / `compactTrailing`) | declared-and-renders (`DynamicIslandExpandedRegion` leading/center/trailing/bottom) | declared-and-renders (`minimal`) | Yes | No in the extension: `ClusterHealthActivityDriver` polls and pushes |
| `BoardProgressActivityAttributes` | declared-and-renders (`LockScreenBoardProgressView`) | declared-and-renders (`compactLeading` / `compactTrailing`) | declared-and-renders (`DynamicIslandExpandedRegion` leading/center/trailing/bottom) | declared-and-renders (`minimal`) | Yes | No in the extension: `BoardProgressActivityDriver` polls and pushes |
| `WakingActivityAttributes` | declared-and-renders (`LockScreenWakeView`) | declared-and-renders (`compactLeading` / `compactTrailing`) | declared-and-renders (`DynamicIslandExpandedRegion` leading/center/bottom) | declared-and-renders (`minimal`) | No — hosted by the separate `HSCCLiveActivity` extension | No in the extension: `LiveActivityManager` polls every 30 s and pushes |

Sources: `ios-app/Sources/HSCCLiveActivityMonitor/{MonitorLiveActivity,CardsRunningLiveActivity,ClusterHealthLiveActivity,BoardProgressLiveActivity}.swift`,
`ios-app/Sources/HSCCLiveActivity/HSCCLiveActivity.swift`, and
`ios-app/Sources/HSCC/{MonitorActivityDriver,CardsRunningActivityDriver,ClusterHealthActivityDriver,BoardProgressActivityDriver,LiveActivityManager}.swift`.

## 3. macOS desktop widgets × family

The macOS extension (`HSCCMacWidgets` in `macos-app/project.yml`) compiles the
six iOS widget files by path. On macOS the accessory families are unavailable,
so they are not part of this table. `systemExtraLarge` is declared only by
Cluster and Monitor.

| Widget | systemSmall | systemMedium | systemLarge | systemExtraLarge | Source |
|---|---|---|---|---|---|
| Cluster | declared-and-renders (`SmallClusterWidget`) | declared-and-renders (`MediumClusterWidget`) | declared-and-renders (`LargeClusterWidget`) | declared-and-renders (`LargeClusterWidget`) | `ios-app/Sources/HSCCWidgets/ClusterWidgetViews.swift` |
| Monitor | declared-and-renders (`SmallMonitorWidget`) | declared-and-renders (`MediumMonitorWidget`) | declared-and-renders (`LargeMonitorWidget`) | declared-and-renders (`LargeMonitorWidget`) | `ios-app/Sources/HSCCWidgets/MonitorViews.swift` |
| Projects | declared-and-renders (`SmallProjectsWidget`) | declared-and-renders (`MediumProjectsWidget`) | declared-and-renders (`LargeProjectsWidget`) | absent | `ios-app/Sources/HSCCWidgets/ProjectsWidget.swift` |
| CardsRunning | declared-and-renders (`CardsRunningSmallView`) | declared-and-renders (`CardsRunningMediumView`) | declared-and-renders (`CardsRunningLargeView`) | absent | `ios-app/Sources/HSCCWidgets/CardsRunningWidget.swift` |
| KanbanMonitor | declared-and-renders (`SmallKanbanMonitor`) | declared-and-renders (`MediumKanbanMonitor`) | declared-and-renders (`LargeKanbanMonitor`) | declared-and-renders (`LargeKanbanMonitor`) | `ios-app/Sources/HSCCWidgets/KanbanMonitorWidget.swift` |
| BoardStatus | declared-and-renders (`BoardStatusSmallView`) | declared-and-renders (`BoardStatusMediumView`) | declared-and-renders (`BoardStatusLargeView`) | absent | `ios-app/Sources/HSCCWidgets/BoardStatusWidget.swift` |

The three remaining `absent` XL cells are the documented macOS family split, not
a defect: all six widgets ship small/medium/large, and Cluster, Monitor and
Kanban Monitor also ship `systemExtraLarge`. Kanban Monitor gained XL in CARD B3
because the per-board table is the surface that benefits from the extra rows.

## 4. Compile results

Commands, run in this worktree:

```text
$ bash ios-app/scripts/build_check.sh
HSCC: 73 files, 0 error(s), 0 warning(s)
HSCCWidgets: 18 files, 0 error(s), 0 warning(s)
HSCCLiveActivity: 5 files, 0 error(s), 0 warning(s)
HSCCLiveActivityMonitor: 13 files, 0 error(s), 0 warning(s)
full compile clean, 0 warnings (compile only — never built or run on a device)
```

```text
$ bash macos-app/scripts/build_check.sh
HSCCMac: 20 files, 0 error(s), 0 warning(s)
HSCCMacWidgets: 18 files, 0 error(s), 0 warning(s)
full compile clean, 0 warnings (compile only — never built or run)
```

## 5. Decode check

```text
$ bash ios-app/scripts/model_decode_check.sh
compiling real models + harness into a macOS CLI...
ALL DECODE CHECKS PASSED — 93/93 checks (over 56 committed fixtures) decoded against the real models
```

## 5a. Cluster/Monitor render check

```text
$ bash ios-app/scripts/cluster_widget_check.sh
CLUSTER WIDGET CHECK PASSED — 56 checks: fixture rows, every family's lines, nil-is-not-zero, redaction
$ bash ios-app/scripts/monitor_activity_check.sh
PASS: real /v1/cluster/monitor + /v1/daemon/host decode + derive to a populated ContentState
```

The cluster fixture is a scrubbed capture of the live `/v1/cluster/monitor`
body (documentation `192.0.2.x` addresses, generic node/model/workload names,
`serving[]` present). The check asserts the exact lines each family draws:

| Family | Headline | Detail | Rows |
|---|---|---|---|
| systemSmall | `4/4 up` | `2 models · 12.3 t/s` | none (one number) |
| systemMedium | `4/4 up` | `2 models · 2 jobs · 12.3 t/s` | 4 compact node rows |
| systemLarge / XL | `4/4 up` | models · jobs · t/s | every node, + CPU/GPU temp and run/wait |
| accessoryInline | `4/4 up · 2 models · 12.3 t/s` | — | none |
| accessoryCircular | `4/4` gauge | — | none |
| accessoryRectangular | `4/4 up` | models · jobs · t/s | first node row |

Every token maps to `WIDGET_ROUTES.md`: tally = `hosts[].error`,
models/throughput = `serving[].model` / `serving[].gen_tps`, jobs =
`workloads[].workload_id` (and `status.workloads.count`), temperatures =
`sample.cpu_temp_c` / `sample.gpu_temp_c`, daemon health =
`daemon_running` / `daemon_uptime_seconds`.

## 5b. Widget execution checks

```text
$ bash ios-app/scripts/board_status_widget_check.sh
104 assertions, 0 failure(s)

$ bash ios-app/scripts/projects_widget_check.sh
PROJECTS WIDGET CHECK PASSED

$ bash ios-app/scripts/kanban_monitor_widget_check.sh
29 assertions, 0 failure(s)
```

The board-status and projects counts are unchanged by CARD B3; the board harness
gained no assertions, only updated expectations for the renamed Done 24h tile
and the ring's running/done fallback. The Kanban Monitor check is new: it decodes
a scrubbed live capture of `GET /v1/mobile/summary` and asserts every drawn
number equals its fixture field, including `done` = Done 24h.

## 6. Shipped feature checklist

| Feature | PR | Merge commit on `main` | Evidence |
|---|---|---|---|
| Multi-address clusters with automatic failover | #371 | `64f8361f` | `ios-app/Sources/Shared/SharedStore.swift`, `ios-app/Sources/Shared/WidgetDataClient.swift`, `ios-app/Sources/HSCC/SettingsStore.swift` |
| Block-H app icon and black/orange dark-only theme | #372 | `e0b88ff7` | `ios-app/Sources/HSCC/Views/Theme.swift`, `ios-app/Sources/HSCC/Views/HSCCMark.swift`, `ios-app/assets-master/appicon-*.svg` |
| Widget style model, App Group storage and appearance editor | #373 | `8bab4521` | `ios-app/Sources/HSCC/WidgetSettingsStore.swift`, `ios-app/Sources/HSCC/Views/WidgetAppearanceView.swift`, `ios-app/Sources/Shared/SharedStore.swift` |
| Widget style applied to every widget and Live Activity | #375 | `14b080dc` | all six widget roots under `ios-app/Sources/HSCCWidgets/` and all five Live Activity configurations; `ios-app/scripts/widget_style_check.sh` |
| macOS menu-bar app | #374 | `d9b78273` | `macos-app/Sources/HSCCMac/` (menu-bar-only app, `LSUIElement`) |
| macOS desktop widgets | #376 | `a3da0180` | `HSCCMacWidgets` target in `macos-app/project.yml`; `ios-app/Sources/HSCCWidgets/WidgetRendering.swift` |
| GitHub Pages OTA install | #377 | `b5691c09` | `ios-app/scripts/publish_pages.sh`, `ios-app/scripts/make_manifest.py` |
| Notarized release and Homebrew cask | #379 | `f8325093` | `macos-app/scripts/release.sh`, `scripts/templates/hscc-widgets.cask.rb.tmpl`, `scripts/bump_cask.py` |

## 7. Gaps

No GAP rows were found in the widget-family or Live Activity matrices. Every
declared family has a dedicated non-placeholder renderer, every Live Activity
type has all four surfaces (Lock Screen, compact, expanded, minimal), and both
platform build checks are clean.

Non-gap coverage notes:

- The monitor extension does not refresh Live Activities. That is the platform
  contract, not a missing path: all five activities are refreshed by app-side
  drivers that poll and push.
- `WakingActivityAttributes` belongs to the separate `HSCCLiveActivity`
  extension rather than `HSCCLiveActivityMonitor`; it is still covered on all
  four surfaces.
- The Cluster tile no longer draws a kanban board row. That row was not
  `hscc cluster monitor` / `hscc cluster status` output; the board surfaces own
  it. The removed read also brings the tile back to two cluster requests.
- The Monitor tile no longer computes a fleet CPU average for its circular
  family. A circular now shows the answered/listed host tally; the per-node CPU
  numbers remain on the node rows, exactly as the CLI prints them.
- Three macOS widgets (Projects, Cards Running, Board Status) do not offer
  `systemExtraLarge`; Cluster, Monitor and Kanban Monitor do.
