# Widget coverage audit — iOS and macOS

Two measurement passes live on this page, and they are labelled separately
because they were taken at different commits.

- **Sections 1–8** (do the families exist and render at all) were measured on
  the merged tree rebased on `origin/main` (2026-11-18), after the cluster route
  parity fix, the CARD B3 board/project refurbish and the CARD B2 running-cards
  refurbish.
- **Sections 9–13** (which field does which surface draw) were measured later on
  a clean `origin/main` worktree, from the source files named in each cell — see
  section 13 for the commit and what is deliberately excluded.

A family is counted as covered only when it is both
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

> **Sections 9–12 are the field × surface matrix** for `hscc cluster monitor` and
> `hscc cluster status`: one row per field those two verbs print, one column per
> iOS family, Live Activity surface, macOS family and the menu bar dropdown, each
> cell naming the file that draws it. Section 12 is the gap list. Sections 1–8
> answer a different question (does a family exist and render at all) and are
> unchanged by this pass.

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

```text
$ bash ios-app/scripts/build_check.sh
$ bash macos-app/scripts/build_check.sh
```

Both compile the real targets (never a device, never an install) and print one
line per target with its file count and its error/warning count. The counts are
the run's own and are deliberately not transcribed here: they move with every
file a card adds, and a figure in this doc gates nothing. What the run claims is
structural — four iOS targets (`HSCC`, `HSCCWidgets`, `HSCCLiveActivity`,
`HSCCLiveActivityMonitor`) and two macOS targets (`HSCCMac`, `HSCCMacWidgets`),
each expected to report 0 errors and 0 warnings, and a nonzero count fails the
script.

History, labelled as such: the Live Activity monitor target gained one file in
B2 (`Sources/Shared/CardsRunningProjection.swift`, 14 → 15 with its
`WidgetDataClient.swift` dependency): the sliced `RunningCardMetric(row:)`
mapping names the stored row type, and that type's failure vocabulary lives in
the client file. Both are Foundation/Security-only; the extension still never
fetches.

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
$ bash ios-app/scripts/monitor_activity_check.sh
```

Both exit nonzero on a failed expectation; each prints its own tally, which is
not transcribed here for the reason §5 gives.

The cluster fixture is a scrubbed capture of the live `/v1/cluster/monitor`
body (documentation `192.0.2.x` addresses, generic node/model/workload names,
`serving[]` present).

**Which family draws which field is section 10b — it is not repeated here.** It
used to be, as a three-column "headline / detail / rows" table, and that table
went stale the moment the tiles stopped drawing a glance line and started
drawing CLI tables: it still described `systemSmall` as one number and
`systemMedium` as four node rows long after #412 and #408 had replaced both. A
second copy of a matrix cannot be kept honest; the matrix has one home.

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
formatted by the app and labelled as such, and they exist on extra-large only
(§9a records why that is not a CLI-parity claim, and §12 records that they are
the only place those numbers appear anywhere).

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
| systemSmall GPU% sparkline fed by `GET /v1/cluster/history` | t_2681d3de | this PR | `ios-app/Sources/HSCCWidgets/ClusterSmallGrid.swift` (trend model, executed by `scripts/model_decode_check.sh` — `gsmall:trend:*`), `ClusterSmallGridView.swift` (polyline), `ClusterWidget.swift`/`MonitorWidget.swift` (family-gated history read, `scripts/cluster_widget_check.sh` §2-budget proofs) |

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
- Section 8's "no GAP rows" is about *surfaces*: every declared family renders
  something. It is not a statement about *fields* — section 12 lists the fields
  those surfaces do not draw, and there are many.

## 9. Field inventory — what the two CLI verbs actually print

The rows of the matrices in section 10 are not chosen from a wish list; they are
the columns the CLI itself adds, read out of `hscc_daemon/cluster_live.py` and
`hscc_daemon/cluster_render.py`. Two of them (marked *wide-only*) appear only
when the terminal is at least `WIDE_MIN = 110` columns wide, so a narrow terminal
never prints them at all.

**`host_table()` — `cluster_live.py:519`, nine columns in this order.** The
format string is the CLI's own, because it is what the parity goldens pin.

| # | CLI column | wire field | CLI format |
|---|---|---|---|
| H1 | `Host` | `hosts[].host` | the node address, escaped — see §7 of the contract |
| H2 | `Jobs` | `sample.sparkrun_jobs` | `{:.0f}` |
| H3 | `CPU%` | `sample.cpu_usage_pct` | `{:.1f}` |
| H4 | `RAM%` | `sample.mem_used_pct` | `{:.1f}` |
| H5 | `Swap%` | `swap_used_mb / swap_total_mb` | `{:.0f}%`, derived in the CLI |
| H6 | `GPU%` | `sample.gpu_util_pct` | `{:.0f}` — no `%` sign |
| H7 | `CPU Temp` | `sample.cpu_temp_c` | `{:.1f}` + `" C"` |
| H8 | `GPU Temp` | `sample.gpu_temp_c` | `{:.0f}` + `" C"` |
| H9 | `GPU Power` | `sample.gpu_power_w` | `{:.2f}` + `" W"` |

**`serving_table()` — `cluster_live.py:554`, eleven columns; `Routes` and
`Prompt t/s` are wide-only.**

| # | CLI column | wire field | CLI format |
|---|---|---|---|
| S1 | `Node` | `serving[].node` | escaped text |
| S2 | `Port` | `serving[].port` | text |
| S3 | `Model` | `serving[].model` | org prefix stripped, `rsplit("/")` |
| S4 | `Routes` | `serving[].routes` | aliases, `-model` suffix trimmed |
| S5 | `Run` | `serving[].running` | `_load_style` is the CLI's only threshold |
| S6 | `Wait` | `serving[].waiting` | `warn` when non-zero |
| S7 | `KV%` | `serving[].kv` | `100 * kv`, `{:.1f}` |
| S8 | `In` | `serving[].prompt_total` | `fmt_tokens`, cumulative |
| S9 | `Out` | `serving[].gen_total` | `fmt_tokens`, cumulative |
| S10 | `Gen t/s` | `serving[].gen_tps` | `{:.1f}`, rate over the poll interval |
| S11 | `Prompt t/s` | `serving[].prompt_tps` | `{:.0f}`, wide-only |
| S12 | `total (n)` row | per model: Run/Wait/In/Out/`Gen t/s` summed | KV% and Prompt t/s are blank on it |

**`render()` — `cluster_live.py:609` — the lines around the tables.**

| # | CLI line |
|---|---|
| N1 | `hscc cluster monitor — N host(s) — every Ns` / `snapshot (use --live to refresh)` |
| N2 | the `host_error` line (sparkrun itself failed) |
| N3 | `serving — proxy :PROXY_PORT` |
| N4 | `In/Out = tokens since each engine started` |
| N5 | `proxy /v1/model/info unreachable: …` (+ ` (showing last known routes)`) |
| N6 | `no backends behind the proxy` |
| N7 | `Ctrl-C to quit` — interactive-only, meaningless on a passive surface |

**`hscc cluster status` — `cluster_render.py::render_cluster_status`.**

| # | CLI label | wire field |
|---|---|---|
| C1 | `workload` column | `workloads[].name` |
| C2 | `tp` column | `workloads[].tp` |
| C3 | `pp` column | `workloads[].pp` |
| C4 | `container` column | `workloads[].container_id` |
| C5 | `total hosts` | `total_hosts` |
| C6 | `idle hosts` | `len(idle_hosts)` |
| C7 | the dim idle-host list under the summary | `idle_hosts[]` (raw sparkrun status lines) |

### 9a. The fields the card body listed that neither verb prints

The card that asked for this matrix named *GPU power limit, VRAM used/total,
clock, fan, net recv/send, uptime, load 1/5/15* as `cluster monitor` output.
Read against the source, that premise is wrong in a way worth recording, because
it changes what "parity" can mean:

- None of those seven is a `host_table()` or `serving_table()` column.
  `cluster_live.py` prints no VRAM, clock, fan, uptime, load or power-limit value
  anywhere. A surface that shows them is not matching the CLI; it is showing
  **route** fields the CLI never renders. They live in the extra-large detail
  block (`Sources/Shared/ClusterMonitorTables.swift`, `ClusterMonitorHostDetail`)
  and are labelled as the route's, not the CLI's.
- `GPU power limit` is worse than unprinted: it is decoded
  (`gpu_power_limit_w`) and used **only** as the denominator of the GPU-power
  threshold verdict (`ClusterMonitorViewModel.swift:249`). No surface shows the
  limit as a number, so a warn-coloured watt cell is comparing against a figure
  the operator cannot see.
- `net recv/send` (`net_recv_bytes_s` / `net_send_bytes_s`) is not in the
  `/v1/cluster/monitor` payload at all — it is in the *history* tier vocabulary
  (`hscc_daemon/metrics_history.py:115`). The XL net field is the route's
  `sample` net counters where present; it is not a CLI column and not the same
  source as the history route.
- `load 1/5/15` **is** printed by a CLI verb — but not this one.
  `hscc-commands/__init__.py:61,78` print `load` and `VRAM` for `hscc status`
  (the one-glance dashboard), which is a different command from
  `hscc cluster status`. Anyone comparing a tile against
  "`hscc cluster status` output" and expecting a load number will not find one.

Fields in the payload that no CLI verb and no surface print: `gpu_name`,
`mem_total_mb`, `mem_available_mb`, `mem_bufcache_mb`, `cpu_freq_mhz`,
`gpu_mem_clock_mhz`, `gpu_encoder_pct`, `gpu_decoder_pct`,
`sparkrun_job_names`, `serving[].error` as such (the row shows `down` instead).

## 10. Field × surface matrices

### 10a. How to read a cell

| mark | meaning |
|---|---|
| **CLI** | the surface draws the CLI's own cell string — the exact text `host_table()` / `serving_table()` hands `Table.add_row`, produced by `ClusterMonitorViewModel.swift`'s `CLIMonitorHostRow.cells` / `CLIMonitorServingRow.cells` |
| **CLI†** | same, with the one documented override: the Host column draws `displayLabel` — the app's friendly name for that host — instead of the CLI's cell, because §7 forbids a wire address on a surface. The value is the CLI's, the text is not |
| **app** | the value is shown but the app formats it: `FleetFormat` / `String(format:)` in the view. Rounding, sign or unit differ from the CLI cell, so the two surfaces can print different strings for one reading |
| **agg** | a fleet-level reduction (mean/sum/max) of that field, which the CLI prints nowhere — it is the app's number, not a CLI cell |
| **—** | not shown |
| **?** | unknown; the reason is in the cell |

File tags (each cell names the file that decides what appears):

| tag | path |
|---|---|
| `SmallGrid` | `ios-app/Sources/HSCCWidgets/ClusterSmallGrid.swift` (rules, Foundation-only) + `ClusterSmallGridView.swift` (draws) |
| `MedTable` | `ios-app/Sources/HSCCWidgets/CLIMonitorTableReading.swift` (rules) + `CLIMonitorHostTableView.swift` (draws) |
| `Tables` | `ios-app/Sources/Shared/ClusterMonitorTables.swift` (plan: which columns, how many rows) + `ios-app/Sources/HSCCWidgets/ClusterMonitorTableViews.swift` (draws) |
| `Fleet` | `ios-app/Sources/Shared/ClusterNodeTally.swift` — `FleetProjection.text(for:)`, the pre-existing glance text |
| `VM` | `ios-app/Sources/Shared/ClusterMonitorViewModel.swift` — the only formatter of CLI cells |
| `ChLA` | `ios-app/Sources/Shared/ClusterHealthActivity.swift` (rules) + `ios-app/Sources/HSCCLiveActivityMonitor/ClusterHealthLiveActivity.swift` (draws) |
| `MonLA` | `ios-app/Sources/Shared/MonitorActivity.swift` (state) + `ios-app/Sources/HSCCLiveActivityMonitor/MonitorLiveActivity.swift` (draws) |
| `MacDrop` | `macos-app/Sources/HSCCMac/DashboardFleetReading.swift` (rules) + `DashboardFleetSection.swift` (draws) |
| `MacTitle` | `macos-app/Sources/HSCCMac/MenuBarLabel.swift` + `MenuBarFleet` in `DashboardFleetReading.swift` |

A mark with no tag means no file draws that field on that surface at all.

### 10b. iOS widget families (Cluster and Monitor tiles — the same pair of files
serves both widgets in every row below; `ClusterWidgetViews.swift` and
`MonitorViews.swift` are the dispatch)

| field | systemSmall | systemMedium | systemLarge | systemExtraLarge | accessoryRectangular | accessoryCircular | accessoryInline |
|---|---|---|---|---|---|---|---|
| H1 Host | CLI† `SmallGrid` | CLI† `MedTable` | CLI† `Tables` | CLI† `Tables` | — | — | — |
| H2 Jobs | — | CLI `MedTable` | CLI `Tables` | CLI `Tables` | agg `Fleet` (`N jobs` = count of `hosts[].workloads[].workload_id` fleet-wide, not the CLI's per-host cell) | — | — |
| H3 CPU% | — | CLI `MedTable` | CLI `Tables` | CLI `Tables` | — | — | — |
| H4 RAM% | — | CLI `MedTable` | CLI `Tables` | CLI `Tables` | — | — | — |
| H5 Swap% | — | CLI `MedTable` | CLI `Tables` | CLI `Tables` | — | — | — |
| H6 GPU% | CLI `SmallGrid` | CLI `MedTable` | CLI `Tables` | CLI `Tables` | app `Fleet` (`GPU 48%`, `FleetFormat.pct` — the CLI prints `48` with no sign) | — | — |
| H7 CPU Temp | CLI `SmallGrid` | CLI `MedTable` | CLI `Tables` | CLI `Tables` | — | — | — |
| H8 GPU Temp | CLI `SmallGrid` | CLI `MedTable` | CLI `Tables` | CLI `Tables` | app `Fleet` (`49°C`, rounded integer + `°C`; the CLI prints `49 C`) | — | — |
| H9 GPU Power | CLI `SmallGrid` | CLI `MedTable` | CLI `Tables` | CLI `Tables` | — | — | — |
| S1 Node | — | — | CLI `Tables` | CLI `Tables` | — | — | — |
| S2 Port | — | — | CLI `Tables` | CLI `Tables` | — | — | — |
| S3 Model | app `SmallGrid` (`models` count in the totals line, `Fleet` elsewhere) | — | CLI `Tables` | CLI `Tables` | agg `Fleet` (`2 models`, a count) | — | agg `Fleet` (count) |
| S4 Routes | — | — | — | — | — | — | — |
| S5 Run | CLI `SmallGrid` (totalled) | — | CLI `Tables` | CLI `Tables` | — | — | — |
| S6 Wait | CLI `SmallGrid` (totalled) | — | CLI `Tables` | CLI `Tables` | — | — | — |
| S7 KV% | — | — | CLI `Tables` | CLI `Tables` | — | — | — |
| S8 In | — | — | CLI `Tables` | CLI `Tables` | — | — | — |
| S9 Out | — | — | CLI `Tables` | CLI `Tables` | — | — | — |
| S10 Gen t/s | — | — | — | — | app `Fleet` (`12.3 t/s`, the CLI's wide-row total, app-rounded) | — | app `Fleet` (`12.3 t/s`) |
| S11 Prompt t/s | — | — | — | — | — | — | — |
| S12 `total (n)` | agg `SmallGrid` (run/wait + `models` only; the per-model rows are collapsed into one line) | — | CLI `Tables` (drawn as rows) | CLI `Tables` (drawn as rows) | — | — | — |
| N1 `N host(s)` | agg `SmallGrid` (host tally `3/4`-style + `+N` for the clipped rows) | — | agg `Fleet` (`4/4 up`) | agg `Fleet` | agg `Fleet` | agg `Fleet` (the headline is all this family draws) | agg `Fleet` |
| N2 host_error line | app (gate pane replaces the grid) | app (replaces the table) | app `Fleet` footer | app `Fleet` footer | app `Fleet` | app `Fleet` | app `Fleet` |
| N3 `serving — proxy :PORT` | — | — | — | — | — | — | — |
| N4 `In/Out = tokens since…` | — | — | — | — | — | — | — |
| N5 proxy-unreachable line | — | — | — | — | — | — | — |
| N6 `no backends behind the proxy` | — (a nil serving section draws nothing here) | — | — (an absent section draws no table) | — | — | — | — |
| C1 workload name | — | — | — | — | — | — | — |
| C2 tp | — | — | — | — | — | — | — |
| C3 pp | — | — | — | — | — | — | — |
| C4 container | — | — | — | — | — | — | — |
| C5 total hosts | app (only in the no-monitor-data fallback line, `N hosts`) | same | same | same | same | same | same |
| C6 idle hosts | — | — | — | — | — | — | — |
| C7 idle host list | — | — | — | — | — | — | — |
| XL-only route fields (VRAM, GPU clock, net, uptime, fan, load) | — | — | — | app `Tables` (`ClusterMonitorHostDetail`; the CLI prints none of these) | — | — | — |

Row counts a plan allows: small 4 hosts (`SmallGrid.maxHosts`), medium 4
(`MedTable.mediumRowLimit`), large 8 hosts / 8 serving (`Tables` plan for
`.large`), XL 8 hosts / 12 serving plus one detail line per visible host. A
clipped block prints `+N more` (`Tables.overflowLine`) rather than reading as a
smaller fleet.

**The one thing this matrix makes visible:** `systemLarge` and
`systemExtraLarge` also carry a `Fleet` header line (`4/4 up` +
`2 models · 3 jobs · 12.3 t/s`) above the tables, so those two surfaces show
*both* an app-formatted fleet summary and the CLI's own cells for the same
reading. Where they disagree — `GPU 48%` vs the CLI's `48`, `49°C` vs `49 C` —
the CLI cell is the parity artefact and the `Fleet` line is decoration the
widget had before the tables existed.

### 10c. Live Activity surfaces

There are two cluster Live Activities. `ClusterHealthActivityAttributes` is the
CLI-parity one. `MonitorActivityAttributes` is the older activity: it still
carries its own `NodeMetric` doubles and formats them in the view, so its cells
are `app`, not `CLI` — the card that moves it onto the shared view-model
(PR #423) is **open, not merged**, so at this commit the Monitor activity does
not draw CLI cells on any surface.

| field | CH lock screen | CH compact | CH expanded | CH minimal | Mon lock screen | Mon compact | Mon expanded | Mon minimal |
|---|---|---|---|---|---|---|---|---|
| H1 Host | CLI† `ChLA` | — | CLI† `ChLA` | — | app `MonLA` (`node.label`, `.NNN` tail) | — | app `MonLA` | — |
| H2 Jobs | — | — | — | — | — | — | — | — |
| H3 CPU% | — | — | — | — | app `MonLA` (`MBar`, `%.0f`) | — | app `MonLA` | — |
| H4 RAM% | — | — | — | — | app `MonLA` (`MBar`) | — | app `MonLA` | — |
| H5 Swap% | — | — | — | — | — | — | — | — |
| H6 GPU% | CLI `ChLA` | agg `ChLA` (peak over hosts, `CLIMonitorFormat.cell(…, "%")`) | CLI `ChLA` | agg `ChLA` (same peak rule) | app `MonLA` (`MBar`) | — | app `MonLA` | — |
| H7 CPU Temp | — | — | — | — | app `MonLA` (`CPU 49°C` — the CLI prints `49.3 C`: one decimal, `" C"`) | — | app `MonLA` | — |
| H8 GPU Temp | CLI `ChLA` | agg `ChLA` (peak temp when no GPU% exists) | CLI `ChLA` | agg `ChLA` | app `MonLA` (`GPU 49°C`) | — | app `MonLA` | — |
| H9 GPU Power | CLI `ChLA` (per host) + agg (total W) | — | CLI `ChLA` + agg (total W) | — | — | — | — | — |
| S3 Model | app `ChLA` (`feedText`) | — | app `ChLA` | — | app `MonLA` | — | app `MonLA` | — |
| S5 Run | agg `ChLA` (`servingLoadText`, `N run`) | — | agg `ChLA` | — | — (decoded into `NodeMetric.running`, never drawn) | — | — | — |
| S6 Wait | agg `ChLA` (`N wait`) | — | agg `ChLA` | — | — (decoded into `NodeMetric.waiting`, never drawn) | — | — | — |
| S7 KV% | — | — | — | — | — | — | — | — |
| S8 In | — | — | — | — | — | — | — | — |
| S9 Out | — | — | — | — | — | — | — | — |
| S10 Gen t/s | app `ChLA` (`feedText`, `%.1f t/s`) | — | app `ChLA` | — | app `MonLA` (`%.1f t/s`) | — | app `MonLA` | — |
| S12 `total (n)` | agg `ChLA` (run/wait totals only, one line) | — | agg `ChLA` | — | — | — | — | — |
| N1 host tally | agg `ChLA` (`tallyText`) | agg `ChLA` (`compactTrailing`, `3/4`, clipped to `9+`) | agg `ChLA` (in the headline) | fallback only (when no load metric exists) | agg `MonLA` (`tallyText`) | agg `MonLA` (`tallyText` — the only number in the compact region) | — | — |
| N3/N5 serving-proxy lines | — | — | — | — | — | — | — | — |
| C1–C7 (`cluster status`) | — | — | — | — | — | — | — | — |

Two surfaces are honest-but-empty by rule, not by omission: **CH minimal** and
**Mon minimal** draw one glyph plus at most one primary metric, and
**Mon compact-leading** is a glyph only. A Dynamic Island region that would hold
"nothing" is where the app deliberately puts no number.

`Mon minimal` shows **no metric at all** on this commit (a state dot only) — the
"one honest primary" rule for minimal is PR #423's, still open.

### 10d. macOS surfaces

`HSCCMacWidgets` compiles the iOS widget files **by path**
(`macos-app/project.yml`), so the four desktop families draw the identical views
and the identical cells as their iOS namesakes; `HSCCMacWidgets` has no source
files of its own but an `Info.plist`. `macos-app/scripts/widget_parity_check.sh`
fails if the two shared-source sets diverge by path, and fails if a `system*`
family ever moves behind `#if os(iOS)`. Only the three accessory families are
iOS-only (they are not offered by macOS WidgetKit), which is why they have no
column here.

| field | desktop small | desktop medium | desktop large | desktop XL | menu bar dropdown | menu bar title |
|---|---|---|---|---|---|---|
| H1 Host | CLI† `SmallGrid` | CLI† `MedTable` | CLI† `Tables` | CLI† `Tables` | CLI† `MacDrop` | — |
| H2 Jobs | — | CLI `MedTable` | CLI `Tables` | CLI `Tables` | — | — |
| H3 CPU% | — | CLI `MedTable` | CLI `Tables` | CLI `Tables` | — | — |
| H4 RAM% | — | CLI `MedTable` | CLI `Tables` | CLI `Tables` | — | — |
| H5 Swap% | — | CLI `MedTable` | CLI `Tables` | CLI `Tables` | — | — |
| H6 GPU% | CLI `SmallGrid` | CLI `MedTable` | CLI `Tables` | CLI `Tables` | CLI `MacDrop` | agg `MacTitle` (mean over reporting hosts) |
| H7 CPU Temp | CLI `SmallGrid` | CLI `MedTable` | CLI `Tables` | CLI `Tables` | CLI `MacDrop` | agg `MacTitle` (hottest, when the CPU sensor wins) |
| H8 GPU Temp | CLI `SmallGrid` | CLI `MedTable` | CLI `Tables` | CLI `Tables` | CLI `MacDrop` | agg `MacTitle` (hottest, CLI cell + host name in the VoiceOver sentence) |
| H9 GPU Power | CLI `SmallGrid` | CLI `MedTable` | CLI `Tables` | CLI `Tables` | CLI `MacDrop` | agg `MacTitle` (summed W) |
| S1 Node | — | — | CLI `Tables` | CLI `Tables` | CLI `MacDrop` | — |
| S2 Port | — | — | CLI `Tables` | CLI `Tables` | CLI `MacDrop` | — |
| S3 Model | app (`models` count) | — | CLI `Tables` | CLI `Tables` | CLI `MacDrop` | — |
| S4 Routes | — | — | — | — | — | — |
| S5 Run | CLI `SmallGrid` (totalled) | — | CLI `Tables` | CLI `Tables` | CLI `MacDrop` | — |
| S6 Wait | CLI `SmallGrid` (totalled) | — | CLI `Tables` | CLI `Tables` | CLI `MacDrop` | — |
| S7 KV% | — | — | CLI `Tables` | CLI `Tables` | CLI `MacDrop` | — |
| S8 In | — | — | CLI `Tables` | CLI `Tables` | CLI `MacDrop` | — |
| S9 Out | — | — | CLI `Tables` | CLI `Tables` | CLI `MacDrop` | — |
| S10 Gen t/s | — | — | — | — | — | — |
| S11 Prompt t/s | — | — | — | — | — | — |
| S12 `total (n)` | agg `SmallGrid` | — | CLI `Tables` | CLI `Tables` | CLI `MacDrop` (total rows are drawn as rows, never capped behind backends) | — |
| N1 `N host(s)` | agg `SmallGrid` | — | agg `Fleet` | agg `Fleet` | agg `MacDrop` (`Copy.hosts(hostCount)`) | agg `MacTitle` (`hostCount`, in the VoiceOver sentence) |
| N3 `serving — proxy :PORT` | — | — | — | — | — (the pane's header is a bare `serving`; the CLI's line carries the proxy port, and this is the only place the port could be shown) | — |
| N4 `In/Out = tokens since…` | — | — | — | — | CLI `MacDrop` (`Copy.tokensNote`, the CLI's words verbatim, beside the serving header) | — |
| N5 proxy-unreachable line | — | — | — | — | CLI `MacDrop` (`Copy.proxyUnreachable`, including the CLI's ` (showing last known routes)` suffix; the dropdown also has one line that is the app's own, `serving poll failed: …`, because `serving_error` has no line in `render()`) | — |
| N6 `no backends behind the proxy` | — (a nil serving section draws nothing here) | — | — | — | CLI `MacDrop` (`Copy.noBackends`) | — |
| C1 workload name | — | — | — | — | CLI `MacDrop` (`DashboardWorkloadRow.name`) | — |
| C2 tp | — | — | — | — | CLI `MacDrop` | — |
| C3 pp | — | — | — | — | CLI `MacDrop` | — |
| C4 container | — | — | — | — | — | — |
| C5 total hosts | app (fallback line only) | same | same | same | CLI `MacDrop` (`Copy.totalHosts`, the CLI's own words) | — |
| C6 idle hosts | — | — | — | — | CLI `MacDrop` (`Copy.idleHosts`) | — |
| C7 idle host list | — | — | — | — | — (deliberate: those strings are raw sparkrun status lines carrying node addresses) | — |
| host `used_slots` / `free_slots` | — | — | — | — | app `MacDrop` (`DashboardNodeSlot`, `N used / N free`) | — |
| XL-only route fields | — | — | — | app `Tables` | — | — |

Dropdown row budgets: 6 hosts, 6 serving rows, 4 workloads, 6 slot rows
(`MacDrop.hostRowLimit` / `servingRowLimit`, `ClusterStatusReading.workloadRowLimit`
/ `slotRowLimit`); the hidden workloads are counted in the VoiceOver sentence,
not silently dropped.

The menu bar **title** is a two-field additive line chosen in Settings
(`.fleetGpu` = GPU load + watts, `.hottestTemp` = hottest temperature + watts,
plus the older `.clusterNodes` tally and `.runningCards` count). It is a
reduction surface: it shows no per-host row and no per-backend row.

## 11. Cells that are `unknown`, and why

No cell above is `unknown` in the sense of "not read": every mark came from a
file in this checkout. Two cells are unknown in a stronger, honest sense, and
naming them is better than a confident dash:

| cell | why unknown |
|---|---|
| every cell, all surfaces, **as pixels** | nothing has rendered any of this. Every gate here is a compile or a Foundation-only harness; the simulator path is blocked by a revoked certificate and no device is attached. Whether a nine-column table actually *fits* `systemMedium` on a given device, or truncates, is unmeasured. The `+N more` rules say what the app intends; only a render says what a user sees. |
| `N6 no backends behind the proxy` **on iOS** | the CLI prints this line when the serving list is empty, and `MacDrop` reproduces it verbatim (`Copy.noBackends`). No iOS surface does. Each instead distinguishes "the route sent no `serving` key" from "the route sent an empty list" — a distinction the CLI does not draw, and one a phone user cannot see either way. Whether iOS should adopt the desktop's line, or keep its silence, is a decision this card cannot make. |
| `ClusterMonitorTables.hasServingSection` | `Sources/Shared/ClusterMonitorTables.swift:367` returns `servingError == nil`, which is true for a clean poll of a response with no `serving` key at all — the same shape `ClusterMonitorViewModel`'s copy fixed (presence captured at projection time). This page reports that the two copies disagree; which one the large/XL tiles should use is a code question, not a docs one, and this card changes no code. |

## 12. Gap list — fields no surface shows

This is the point of the exercise. Ordered by how surprising the gap is.

**Printed by the CLI, drawn by nobody anywhere:**

| field | the CLI prints it | gap |
|---|---|---|
| `Routes` (S4) | `serving_table`, wide-only | no surface has a Routes cell. It is in the plan's column enum (`Tables.ClusterMonitorServingColumn.routes`) and no plan selects it, so the layout can draw it the moment a plan asks. |
| `Prompt t/s` (S11) | `serving_table`, wide-only | same shape as Routes: declared in the enum, selected by no plan, drawn by nothing. |
| `Gen t/s` per backend row (S10) | `serving_table` | the per-row value is drawn nowhere. The **total** appears as an app-formatted `12.3 t/s` on `Fleet` surfaces, and `.large`'s plan drops the column on purpose ("every live row sends null"), so a live per-row rate is invisible everywhere. |
| `container` (C4) | `cluster status` table | no surface draws a container id, on either platform. |
| `idle hosts` (C6) on iOS | `cluster status` | decoded into `ClusterStatusSummary.idleHosts` (`ClusterWidgetMetrics.swift:71,77`) and never read by any view. The macOS dropdown does draw it. |
| `idle hosts` the host **list** (C7) | `cluster status`, as a dim line | drawn nowhere, on either platform. macOS draws the count only — deliberately, because those lines are raw sparkrun status text containing node addresses (§7). |
| `Swap%` (H5) on any surface except the medium/large/XL tables | `host_table` | the small tile's four columns are GPU%/GPU Temp/CPU Temp/GPU Power, so Swap% (and CPU%, RAM%, Jobs) are absent from `systemSmall` on both platforms. |
| `Jobs` (H2) as the CLI prints it | `host_table` | the only fleet-wide "N jobs" on Lock Screen families is `FleetProjection.jobs` — a count of `hosts[].workloads[].workload_id`, a different source than the CLI's `sample.sparkrun_jobs`. Same word, two numbers. |
| `total (n)` per model (S12) on `systemSmall`, the accessories, both Live Activities | `serving_table` | only `Tables` (large/XL) and the macOS dropdown keep the per-model total rows. Everywhere else they are collapsed into one run/wait line, which loses *which model* the queue belongs to. |
| `N1` host-tally wording | title line `— N host(s) —` | no surface prints the CLI's phrase; every surface invents its own tally form (`4/4 up`, `3/4`, `9+`, `4 hosts`). |
| `N3 serving — proxy :PORT` | `render()` | no surface prints this line, so **the proxy port appears nowhere in either app** — yet every serving row on four surfaces came from that proxy, and `MacDrop` is the only surface that even labels the section as `serving`. This is the single most consequential missing note: two clusters behind different proxies render identically. |
| `N4 In/Out = tokens since…` and `N6 no backends behind the proxy` | `render()` | **macOS dropdown only** — `Copy.tokensNote` and `Copy.noBackends` quote the CLI verbatim. On iOS neither line exists, so `In` and `Out` are drawn on `systemLarge`, `systemExtraLarge` and the desktop tiles with no statement that they are cumulative-since-engine-start rather than rates, and an empty backend list on iOS is silence where the CLI says *why*. |
| `N5 proxy /v1/model/info unreachable: …` | `render()` | macOS dropdown only (`Copy.proxyUnreachable`, suffix included). `ClusterMonitorTables.proxyError` is carried by the iOS projection and drawn by **no iOS view** — the phone has the same failure and shows nothing. |
| `N2` the `host_error` line | `render()` | not reproduced as text. Surfaces replace the table with a gate pane instead, which is a different sentence from the CLI's. |
| `N7 Ctrl-C to quit` | `render()` | interactive-only; correct that no passive surface shows it. Recorded so the row is not mistaken for a gap. |

**On the wire, printed by no CLI verb, and drawn by no surface:** `gpu_name`,
`mem_total_mb`, `mem_available_mb`, `mem_bufcache_mb`, `cpu_freq_mhz`,
`gpu_mem_clock_mhz`, `gpu_encoder_pct`, `gpu_decoder_pct`, `sparkrun_job_names`,
`gpu_mem_used_pct` (only used/total are shown, on XL). The `serving[].error`
*reason* is the one exception worth naming: `serving_table` puts the truncated
reason in the **Run** column of a `down` row (`ClusterMonitorViewModel` mirrors
that exactly), and every plan draws Run — so the reason is visible on the tables
and the dropdown, and on no Live Activity or accessory, which draw no per-backend
row at all.

**On the wire, printed by no CLI verb, and drawn by exactly one surface** — each
of these is a single-surface field, so if that surface is clipped it is the only
place the number existed:

| field | only surface |
|---|---|
| `gpu_mem_used_mb` + `gpu_mem_total_mb` (VRAM) | XL detail, both platforms |
| `gpu_clock_mhz` | XL detail |
| `gpu_fan_pct` | XL detail |
| `uptime_sec` | XL detail |
| `cpu_load_1m` / `_5m` / `_15m` | XL detail |
| net ↓/↑ | XL detail |
| `used_slots` / `free_slots` | macOS dropdown only |
| `gpu_power_limit_w` | nowhere as a value — it only sets a colour (§9a) |

**Parity divergences rather than absences** — these fields *are* shown, but not
as the CLI prints them, so a comparison line-by-line against the CLI fails:

| field | surfaces | CLI prints | surface shows |
|---|---|---|---|
| `CPU Temp` (H7) | Monitor Live Activity, all rows | `49.3 C` (`{:.1f}`) | `CPU 49°C` — integer, `°C` suffix (`MonitorLiveActivity.swift`) |
| `GPU Temp` (H8) | accessoryRectangular, Monitor LA | `49 C` | `GPU 49°C` |
| `GPU%` (H6) | accessoryRectangular, Monitor LA | `93` (no sign) | `GPU 93%` |
| `Run`/`Wait` (S5/S6) | `systemSmall`, both Live Activities | per-backend rows, plus `total (n)` | fleet totals only (`N run · N wait`) |
| `Gen t/s` (S10) | `Fleet` detail lines | per-row + total | total only, app-rounded |
| workload count (C1) | iOS Cluster widget families | one row per workload with tp/pp | a single `N jobs` count |

## 13. Provenance and what would falsify this page

Measured at `origin/main` `af6e0c61` in a scratch worktree (the branch was then
rebased onto a newer `origin/main` with no conflict and no code change, so every
cell still describes merged behaviour), and no code changed — so the matrices
describe merged behaviour only. Three PRs in this family are open
and are therefore **not** in the matrices above as merged behaviour — where the
handoff for one of them claims a surface draws CLI cells, this page follows the
code on `main` instead: #409 (large/XL — its content reached `main` through the
#424 squash, which is why the XL rows are marked CLI), #415 (accessory families
rewritten onto `CLILockScreenMetrics` — until it merges, the accessories are the
`Fleet` app-formatted cells this page records), and #423 (Monitor Live Activity
onto `MonitorActivityMetrics` — until it merges, the Monitor activity's four
surfaces are the `app`/glyph rows recorded here, and its minimal region shows no
metric at all). If any of those three merges, sections 10b, 10c and 12 need a
re-read: the `app` and `—` cells they name are the ones that change.

The gate outputs for this pass — both `build_check.sh` runs and the
`model_decode_check.sh` banner — are in the PR body, not here: those numbers are
produced by the gates, the gates print them, and a doc figure can only lag them
(§5 above says the same for the decode run).

What would falsify a cell: a `build_check.sh` compile error in the named file, or
a `model_decode_check` / `cluster_widget_check` failure against the committed
`/v1/cluster/monitor` fixture. Both run without a device, so both are checked
before this page is trusted. What neither can falsify is the second row of
section 11 — nobody has looked at the pixels.
