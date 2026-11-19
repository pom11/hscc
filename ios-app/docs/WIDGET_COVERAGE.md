# Widget coverage audit — iOS and macOS

Measured on the merged tree rebased on `origin/main` (2026-11-18), after the
cluster route parity fix, the CARD B3 board/project refurbish and the CARD B2
running-cards refurbish. A family is counted as covered only when it is both
listed in that widget's `supportedFamilies` **and** has a non-placeholder view
in the family dispatch switch (or routes through the shared, gated text model
the dispatch selects). The Cluster and Monitor surfaces were refurbished by the
sibling card; the Kanban Monitor, Board Status and Projects widgets plus the
Board Progress Live Activity were refurbished by CARD B3; the Cards widget and
CardsRunning Live Activity by CARD B2. Every surface reads the fields in
`hscc-api/docs/WIDGET_ROUTES.md`. Card R3 adds the "Needs me" widget (the alert
feed) on both platforms and the macOS dropdown's "Needs you" section; it has no
Live Activity.

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
`HSCCWidgetsBundle.swift` contains exactly these seven widgets; there are no
additional `Widget` types in that directory.

| Widget | systemSmall | systemMedium | systemLarge | accessoryInline | accessoryCircular | accessoryRectangular | Source |
|---|---|---|---|---|---|---|---|
| Projects | declared-and-renders (`SmallProjectsWidget`) | declared-and-renders (`MediumProjectsWidget`) | declared-and-renders (`LargeProjectsWidget`) | declared-and-renders (`Text(entry.inlineLine)`) | declared-and-renders (`ProjectsCircularView`) | declared-and-renders (`ProjectsRectangularView`) | `ios-app/Sources/HSCCWidgets/ProjectsWidget.swift` |
| CardsRunning | declared-and-renders (`CardsRunningSmallView`) | declared-and-renders (`CardsRunningMediumView`) | declared-and-renders (`CardsRunningLargeView`) | declared-and-renders (`Text(entry.inlineLine)`) | declared-and-renders (`CardsRunningCircularView`) | declared-and-renders (`CardsRunningRectangularView`) | `ios-app/Sources/HSCCWidgets/CardsRunningWidget.swift` |
| KanbanMonitor | declared-and-renders (`SmallKanbanMonitor`) | declared-and-renders (`MediumKanbanMonitor`) | declared-and-renders (`LargeKanbanMonitor`) | declared-and-renders (`KanbanMonitorInline`) | declared-and-renders (`KanbanMonitorCircular`) | declared-and-renders (`KanbanMonitorRectangular`) | `ios-app/Sources/HSCCWidgets/KanbanMonitorWidget.swift` |
| BoardStatus | declared-and-renders (`BoardStatusSmallView`) | declared-and-renders (`BoardStatusMediumView`) | declared-and-renders (`BoardStatusLargeView`) | declared-and-renders (`Text(entry.inlineLine)`) | declared-and-renders (`BoardStatusCircularView`) | declared-and-renders (`BoardStatusRectangularView`) | `ios-app/Sources/HSCCWidgets/BoardStatusWidget.swift` |
| Cluster | declared-and-renders (`SmallClusterWidget`) | declared-and-renders (`MediumClusterWidget`) | declared-and-renders (`LargeClusterWidget`) | declared-and-renders (`InlineClusterWidget`) | declared-and-renders (`CircularClusterWidget`) | declared-and-renders (`RectangularClusterWidget`) | `ios-app/Sources/HSCCWidgets/ClusterWidgetViews.swift` |
| Monitor | declared-and-renders (`SmallMonitorWidget`) | declared-and-renders (`MediumMonitorWidget`) | declared-and-renders (`LargeMonitorWidget`) | declared-and-renders (`InlineMonitorWidget`) | declared-and-renders (`CircularMonitorWidget`) | declared-and-renders (`RectangularMonitorWidget`) | `ios-app/Sources/HSCCWidgets/MonitorViews.swift` |
| NeedsMe | declared-and-renders (`SmallNeedsMe`) | declared-and-renders (`MediumNeedsMe`) | declared-and-renders (`LargeNeedsMe`) | declared-and-renders (`Text(entry.inlineLine)`) | declared-and-renders (`NeedsMeCircular`) | declared-and-renders (`NeedsMeRectangular`) | `ios-app/Sources/HSCCWidgets/NeedsMeWidget.swift` |

Coverage note: every iOS widget contains a `.systemExtraLarge` branch in its view
switch. **Cluster and Monitor declare `.systemExtraLarge` in `supportedFamilies`
on iOS as well** (t_9e64d9d9): on iPad those two draw the `cluster monitor` host
and serving tables side by side, so the family and the renderer are both live.
The other four widgets still carry the branch without declaring the family, so
for them XL remains **absent** on iOS by configuration, not a missing renderer.

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
so they are not part of this table. `systemExtraLarge` is declared by Cluster,
Monitor and CardsRunning.

| Widget | systemSmall | systemMedium | systemLarge | systemExtraLarge | Source |
|---|---|---|---|---|---|
| Cluster | declared-and-renders (`SmallClusterWidget`) | declared-and-renders (`MediumClusterWidget`) | declared-and-renders (`LargeClusterWidget`) | declared-and-renders (`ExtraLargeClusterWidget`) | `ios-app/Sources/HSCCWidgets/ClusterWidgetViews.swift` |
| Monitor | declared-and-renders (`SmallMonitorWidget`) | declared-and-renders (`MediumMonitorWidget`) | declared-and-renders (`LargeMonitorWidget`) | declared-and-renders (`ExtraLargeMonitorWidget`) | `ios-app/Sources/HSCCWidgets/MonitorViews.swift` |
| Projects | declared-and-renders (`SmallProjectsWidget`) | declared-and-renders (`MediumProjectsWidget`) | declared-and-renders (`LargeProjectsWidget`) | absent | `ios-app/Sources/HSCCWidgets/ProjectsWidget.swift` |
| CardsRunning | declared-and-renders (`CardsRunningSmallView`) | declared-and-renders (`CardsRunningMediumView`) | declared-and-renders (`CardsRunningLargeView`) | declared-and-renders (`CardsRunningLargeView` — XL reuses the large renderer) | `ios-app/Sources/HSCCWidgets/CardsRunningWidget.swift` |
| KanbanMonitor | declared-and-renders (`SmallKanbanMonitor`) | declared-and-renders (`MediumKanbanMonitor`) | declared-and-renders (`LargeKanbanMonitor`) | declared-and-renders (`LargeKanbanMonitor`) | `ios-app/Sources/HSCCWidgets/KanbanMonitorWidget.swift` |
| BoardStatus | declared-and-renders (`BoardStatusSmallView`) | declared-and-renders (`BoardStatusMediumView`) | declared-and-renders (`BoardStatusLargeView`) | absent | `ios-app/Sources/HSCCWidgets/BoardStatusWidget.swift` |
| NeedsMe | declared-and-renders (`SmallNeedsMe`) | declared-and-renders (`MediumNeedsMe`) | declared-and-renders (`LargeNeedsMe`) | absent | `ios-app/Sources/HSCCWidgets/NeedsMeWidget.swift` |

The two remaining `absent` XL cells (Projects and Board Status) are the
documented macOS family split, not a defect: all seven widgets ship
small/medium/large, and Cluster, Monitor, Kanban Monitor (CARD B3) and Cards
Running (CARD B2) also ship `systemExtraLarge`. Needs Me is deliberately
small/medium/large on macOS: its data is a three-to-eight-row list, so an XL
cell would repeat the large renderer rather than add a column.

## 4. Compile results

Commands, run in this worktree:

```text
$ bash ios-app/scripts/build_check.sh
HSCC: 75 files, 0 error(s), 0 warning(s)
HSCCWidgets: 30 files, 0 error(s), 0 warning(s)
HSCCLiveActivity: 5 files, 0 error(s), 0 warning(s)
HSCCLiveActivityMonitor: 20 files, 0 error(s), 0 warning(s)
full compile clean, 0 warnings (compile only — never built or run on a device)
```

The Live Activity monitor target gained one file in B2
(`Sources/Shared/CardsRunningProjection.swift`, 14 → 15 with its
`WidgetDataClient.swift` dependency): the sliced `RunningCardMetric(row:)`
mapping names the stored row type, and that type's failure vocabulary lives in
the client file. Both are Foundation/Security-only; the extension still never
fetches.

```text
$ bash macos-app/scripts/build_check.sh
HSCCMac: 27 files, 0 error(s), 0 warning(s)
HSCCMacWidgets: 30 files, 0 error(s), 0 warning(s)
full compile clean, 0 warnings (compile only — never built or run)
```

## 5. Decode check

```text
$ bash ios-app/scripts/model_decode_check.sh
ALL DECODE CHECKS PASSED — N/N checks (over F committed fixtures) decoded against the real models
```

The run prints its own `N` (checks) and `F` (distinct fixture files it opened);
neither is transcribed here, because both move with every fixture added or
removed and a stale figure in a doc gates nothing. What the section claims is
structural: every committed fixture decodes against the REAL models, some
fixtures carry several checks (so `N` exceeds `F`), and a negative fixture passes
by *failing* to decode.

The two B2 fixtures add 4 checks and 2 files (`v1_kanban_running_cards.json`
and `v1_kanban_running_contract.json`). The live-capture fixture is scrubbed at
the value level only; the contract fixture is shape-faithful from
`kanban_live.CARD_VIEW_FIELDS` because the deployed 2.15.11 server predates that
route shape.

## 5a. Cluster/Monitor render check

```text
$ bash ios-app/scripts/cluster_widget_check.sh
CLUSTER WIDGET CHECK PASSED — the run prints its own check tally
$ bash ios-app/scripts/monitor_activity_check.sh
PASS: real /v1/cluster/monitor + /v1/daemon/host decode + derive to a populated ContentState
```

The cluster fixture is a scrubbed capture of the live `/v1/cluster/monitor`
body (documentation `192.0.2.x` addresses, generic node/model/workload names,
`serving[]` present). The check asserts the exact lines each family draws —
the tally is the run's own output, not this page's:

| Family | Headline | Detail | Rows |
|---|---|---|---|
| systemSmall | `4/4 up` | `2 models · 12.3 t/s` | none (one number) |
| systemMedium | `4/4 up` | `2 models · 2 jobs · 12.3 t/s` | 4 compact node rows |
| systemLarge | `4/4 up` | models · jobs · t/s | the CLI's host table, then its serving table (Node Port Model Run Wait KV% In Out + the per-model `total (n)` rows), each clipped to the plan with a `+N more` line |
| systemExtraLarge | `4/4 up` | models · jobs · t/s | the same two tables side by side, plus one detail line per host — VRAM used/total, GPU clock, net ↓/↑ per second, uptime, fan %, load 1/5/15 |
| accessoryInline | the hottest temperature + the one queue fact that matters | — | none |
| accessoryCircular | fleet GPU load as a capacity ring | what that load costs, in watts | none |
| accessoryRectangular | hottest temp · GPU load · fleet watts | the whole queue + how many hosts the read covers | none |

The three Lock Screen families take their numbers from
`ios-app/Sources/Shared/ClusterLockScreen.swift`, which projects the same CLI
view-model the Home Screen rows do: GPU load is a mean over the hosts that
reported it (so it is labelled `avg` wherever it appears), watts are summed, and
both carry their `n/N` coverage when only part of the fleet reported — except in
the ring, which has no characters to spare, so there the marker survives only in
the VoiceOver sentence and the rectangular row. The strings are that file's, not
this page's: read the `lock-screen` group of the check for what is actually
asserted.

Every token maps to `WIDGET_ROUTES.md`: tally = `hosts[].error`,
models/throughput = `serving[].model` / `serving[].gen_tps`, jobs =
`workloads[].workload_id` (and `status.workloads.count`), temperatures =
`sample.cpu_temp_c` / `sample.gpu_temp_c`, power = `sample.gpu_power_w`,
running/waiting = `serving[].running` / `serving[].waiting`, daemon health =
`daemon_running` / `daemon_uptime_seconds`.

Who owns what on the two table surfaces (t_9e64d9d9): the **cells** are
`ClusterMonitorViewModel`'s (parity with `hscc_daemon/cluster_live.py`, pinned by
the goldens `cluster_monitor_cli_parity.py` generates), the **layout** — which
columns fit, how many rows fit, what the `+N more` line says, and the six
extra-large-only fields — is `Sources/Shared/ClusterMonitorTables.swift`, and the
**painting** is `Sources/HSCCWidgets/ClusterMonitorTableViews.swift`, shared by
the Cluster and Monitor widgets so the two tiles cannot drift. The layout file is
Foundation-only so `model_decode_check.sh` can execute it against the committed
captures; a rule that lived inside a view file could only ever be asserted in
prose. The six detail fields are not CLI columns — `cluster_live.py` prints no
VRAM, clock, net, uptime, fan or load anywhere — so they are the route's fields,
formatted by the app and labelled as such, and they exist on extra-large only.

## 5b. Widget execution checks

```text
$ bash ios-app/scripts/board_status_widget_check.sh
104 assertions, 0 failure(s)

$ bash ios-app/scripts/projects_widget_check.sh
PROJECTS WIDGET CHECK PASSED

$ bash ios-app/scripts/kanban_monitor_widget_check.sh
29 assertions, 0 failure(s)

$ bash ios-app/scripts/cards_running_widget_check.sh
CARDS WIDGET CHECK PASSED — every family's shown values equal the live fixture's fields, and no blind/stale state invents a number
CARDS RUNNING WIDGET CHECK PASSED — every family, both platforms and the Live Activity draw the live fixture's own fields
```

The board-status and projects counts are unchanged by CARD B3; the board harness
gained no assertions, only updated expectations for the renamed Done 24h tile
and the ring's running/done fallback. The Kanban Monitor check is new: it decodes
a scrubbed live capture of `GET /v1/mobile/summary` and asserts every drawn
number equals its fixture field, including `done` = Done 24h.

The Cards Running check is B2's: it compiles the real `SharedModels.swift`,
`SharedStore.swift`, `WidgetDataClient.swift` and
`CardsRunningProjection.swift`, plus the sliced `cards-row-metric` and
`cards-reading-rules` blocks from
`Sources/Shared/CardsRunningActivity.swift`. It asserts one section per family:
iOS Small/Medium/Large and the three accessory families, macOS
Small/Medium/Large/XL, the Live Activity's four surfaces, and the blind/stale
rules (no `0 running` from a blind read, no fresh-looking cache).

## 5c. Needs me widget (card R3)

```text
$ bash ios-app/scripts/model_decode_check.sh
ALL DECODE CHECKS PASSED — 300/300 checks (over 70 committed fixtures) decoded against the real models
```

`v1_alerts.json` is the alert-feed fixture (shape from `routes_alerts.py`, all
text placeholder). The check executes the real `NeedsMeFeed.from` projection and
pins the contract the families draw:

| Family | Headline | Detail | Rows |
|---|---|---|---|
| systemSmall | unacknowledged count | newest title + age | none |
| systemMedium | count + updated age | — | top 3: level, title, age |
| systemLarge | count + updated age | — | up to 8, each with the body first line |
| accessoryInline | `"3 need you"` / `"nothing needs you"` | — | none |
| accessoryCircular | count | — | none |
| accessoryRectangular | newest title | age + count line | none |

Order is newest-first (descending id), the same presentation `hscc notify list`
produces by reversing the route's ascending page. Rows with `acked: true` are
excluded; `hscc notify list` keeps them as history with its `(acked)` marker.
Levels `critical`/`action` use the operator's accent (`#FF5722` by default) and
`info` the secondary role — system-tinted on the Lock Screen. Empty is
`"Nothing needs you"` with the update age; a stale read keeps its real rows and
names the reason and age; a failed read names the reason. The macOS menu bar
dropdown renders the same projection in a "Needs you" section at the top,
hidden when empty.

## 6. Shipped feature checklist

| Feature | PR | Merge commit on `main` | Evidence |
|---|---|---|---|
| Multi-address clusters with automatic failover | #371 | `64f8361f` | `ios-app/Sources/Shared/SharedStore.swift`, `ios-app/Sources/Shared/WidgetDataClient.swift`, `ios-app/Sources/HSCC/SettingsStore.swift` |
| Block-H app icon and black/orange dark-only theme | #372 | `e0b88ff7` | `ios-app/Sources/HSCC/Views/Theme.swift`, `ios-app/Sources/HSCC/Views/HSCCMark.swift`, `ios-app/assets-master/appicon-*.svg` |
| Widget style model, App Group storage and appearance editor | #373 | `8bab4521` | `ios-app/Sources/HSCC/WidgetSettingsStore.swift`, `ios-app/Sources/HSCC/Views/WidgetAppearanceView.swift`, `ios-app/Sources/Shared/SharedStore.swift` |
| Widget style applied to every widget and Live Activity | #375 | `14b080dc` | all seven widget roots under `ios-app/Sources/HSCCWidgets/` and all five Live Activity configurations; `ios-app/scripts/widget_style_check.sh` |
| macOS menu-bar app | #374 | `d9b78273` | `macos-app/Sources/HSCCMac/` (menu-bar-only app, `LSUIElement`) |
| macOS desktop widgets | #376 | `a3da0180` | `HSCCMacWidgets` target in `macos-app/project.yml`; `ios-app/Sources/HSCCWidgets/WidgetRendering.swift` |
| GitHub Pages OTA install | #377 | `b5691c09` | `ios-app/scripts/publish_pages.sh`, `ios-app/scripts/make_manifest.py` |
| Notarized release and Homebrew cask | #379 | `f8325093` | `macos-app/scripts/release.sh`, `scripts/templates/hscc-widgets.cask.rb.tmpl`, `scripts/bump_cask.py` |
| Cards widget + CardsRunning Live Activity refurbish | B2 `t_635bae56` | this PR | `ios-app/Sources/HSCCWidgets/CardsRunningWidget.swift`, `ios-app/Sources/HSCCLiveActivityMonitor/CardsRunningLiveActivity.swift`, `ios-app/Sources/Shared/CardsRunningProjection.swift`, `ios-app/scripts/cards_running_widget_check.sh` |

## 7. Running-cards data mapping (B2)

`hscc cards monitor --short` is the reference. Every number and row the Cards
widget and the CardsRunning Live Activity draw is one field of
`GET /v1/kanban/running` (or, for the secondary blocked figure, the documented
`GET /v1/kanban/blocked`), which `hscc-api/docs/WIDGET_ROUTES.md` binds to
`kanban_live.collect(registered_only=True)` — the same pass the CLI prints.

| Shown value | Route field | CLI source |
|---|---|---|
| headline number / `accessoryCircular` | `count` | `len(collect().running)` — the `slots` number in `hscc cards monitor` |
| row set + order | `tasks[]` (longest run first) | `collect().running`, sorted by `-run_s` |
| title | `tasks[].title` | panel title |
| board | `tasks[].board` | panel header `board` |
| worker | `tasks[].worker`, else `assignee` | panel header `worker` (`r["profile"] or r["assignee"]`) |
| age | `tasks[].run_s`, else `started_at` | panel header `run` |
| secondary blocked warning | `/v1/kanban/blocked` `count` | `hscc kanban blocked` (never the cards headline) |
| freshness | client read time / snapshot time | not a CLI field: rendered as "updated 3m ago" or "last known 3m ago · reason" |

There is no current-step field on the route (`CARD_VIEW_FIELDS` has no step or
tool column), so the widget does not invent one. `hb_s` is decoded and kept for
a future heartbeat label but is not rendered, because the card specifies
title/board/worker/age and `run_s` is the age the CLI draws.

## 8. Gaps

No GAP rows were found in the widget-family or Live Activity matrices. Every
declared family has a dedicated non-placeholder renderer, every Live Activity
type has all four surfaces (Lock Screen, compact, expanded, minimal), and both
platform build checks are clean.

Non-gap coverage notes:

- Notifications are out of scope by operator decision (2026-11-19): the apps
  carry neither local nor push notifications. The widgets and Live Activities
  in this document are unaffected — they are not notifications.
- The monitor extension does not refresh Live Activities. That is the platform
  contract, not a missing path: all five activities are refreshed by app-side
  drivers that poll and push.
- `WakingActivityAttributes` belongs to the separate `HSCCLiveActivity`
  extension rather than `HSCCLiveActivityMonitor`; it is still covered on all
  four surfaces.
- Two macOS widgets (Projects and Board Status) do not offer
  `systemExtraLarge`; Cluster, Monitor, Kanban Monitor (CARD B3) and Cards
  Running (CARD B2) do.
