# Widget redesign — apps 1.4 (operator picks, 2026-11-20)

Visual reference: `mock.html` in this folder (open the source; every layout is a
JS render function named after its id, e.g. `C1`, `B3`). Commit both this file and
the mock into the repo as `docs/design/2026-11-20-widget-redesign.md` and
`docs/design/2026-11-20-widget-redesign-mock.html` in the FIRST card's PR. That
doc is the contract for every later card.

## Operator picks

| Area | Pick |
|---|---|
| Widget set | **Consolidate to 5**: Cluster, Boards, Running, Needs Me, Merge Queue |
| Look | **Native**: system colors, SF Pro Rounded for numbers/headlines, color only for state |
| Cluster | **C1 Health line** (small, medium, large) |
| Boards | **B3 Stacked bars** |
| Running | **R3 Longest** |
| Needs Me | **N3 Calm when clear** |
| Merge Queue | **M1 Pipeline** |
| Lock screen | **L2 Gauges** |
| Live Activity | **A2 Cluster watch** (the only Live Activity left) |

## Consolidation map

| New widget | Replaces | Keep this WidgetKit `kind` (so placed widgets survive) |
|---|---|---|
| Cluster | Cluster, HSCC Monitor, Cluster Graph | the existing Cluster widget kind |
| Boards | Kanban Monitor, Board Status, Projects | the existing Kanban Monitor kind |
| Running | Running Cards | existing |
| Needs Me | Needs Me | existing |
| Merge Queue | Merge Queue | existing |

Removed widgets leave the bundle, the registry (`ios-app/widgets/*.json` +
`widget_registry.py --write`), the settings screen, and every gate that lists
them. Delete their Swift files only when nothing else uses the code (shared
readers such as `ClusterHistorySeries` stay if Cluster large uses them).
Live Activities: keep ONE, "Cluster watch" (reuse the Cluster Health activity's
plumbing); remove Wake, Monitor, Running Cards and Board Progress activities,
their drivers and their settings rows.

## Native look (shared kit, one file, used by every widget)

- Background: `containerBackground(NativeStyle.surface, for: .widget)`, a solid card: white in light mode, #1C1C1E in dark, as in the mock. (1.4.0 shipped the translucent `.fill.tertiary`; the operator review on 2026-11-20 rejected it.)
- Ink: `.primary`, secondary text `.secondary`. Accent `.blue` (tint-aware).
- State colors only: ok `.green`, warn `.orange`, bad `.red`. Temperature: <70 ok, 70–74 warn, ≥75 bad.
- Type: label 11 pt semibold `.secondary`; hero 34 pt bold `.rounded`; sub 13 pt semibold; small 11 pt. Numbers `.monospacedDigit()`.
- Components: `AsOfStamp` (HH:mm of the data, not of the render), `MeterBar` (6 pt capsule), `Tile` (12 pt radius, `.quaternary` fill), `StaleModifier`.
- **Staleness**: every widget shows the data's own timestamp. When data is older than 2× the widget's refresh interval, dim content to 50% and show "stale · HH:mm"; when the last fetch failed, show "offline · HH:mm" with the cached data dimmed. Never show cached data as fresh.

## Layouts

Sizes: small 158 pt, medium 338×158, large 338×354 (iPhone); macOS uses the same views.

**Cluster C1 Health line**
- small: label "Cluster"; hero "All up" green (or "3/4 up" orange, "Down" red); "N tok/s"; "Hottest T°" colored by temp; as-of.
- medium: left column (112 pt): "Cluster", hero "4/4", "serving", "N tok/s", as-of. Right: one row per node: short name (S1…), util meter (orange >85%), temp colored.
- large: header "Cluster · 4/4 up" + as-of; hero tok/s "now"; 2 h total-throughput area chart from `/v1/cluster/history` (left "2 h ago", right "now"); node table: node (◆ = orchestrator), util meter, temp, power W, tok/s; footer: model name ×count, daemon state.
- accessoryCircular (L2): Gauge of hottest node temp (0–90 °C), label node short name.
- accessoryRectangular (L2): "CLUSTER · N TOK/S" + four node temps in a row.
- accessoryInline: "4/4 up · N tok/s".

**Boards B3 Stacked bars** (scope: all boards, or one board via the existing scope intent)
- small: "Open cards"; top 4 boards by open count: name + total, bar split running (accent) / todo (secondary) / blocked (orange), bar length ∝ open cards.
- medium: "Open cards by board" + as-of; 4 rows: name (100 pt), stacked bar, "N done" today; legend row.
- large: all boards (up to ~10) as medium rows + worker slots strip "R of 6 slots" (cap from config) + totals footer "D done today · B blocked".
- One-board scope: same bar for that board's columns + running card titles.
- accessoryCircular (L2): Gauge running/slots ("2/6 slots"). Inline: "2 running · 38 blocked".

**Running R3 Longest**
- small: "Longest running"; hero elapsed ("32m", "1h 05m"); title (2 lines); heartbeat dot: green "beat Ns" if < 5 min, orange "beat Nm" if older (from `/v1/kanban/stale` / heartbeat age).
- medium: left: "Longest", hero, heartbeat; "+N more"; right: title (2 lines), card id · worker.
- large: hero block as medium, then the other running cards as rows (title, elapsed, heartbeat).
- Empty: "Nothing running" calm, with "last finished HH:mm" if known.
- Inline/rectangular: "32m · <title>".

**Needs Me N3 Calm when clear**
- Empty: small/medium show green "All clear" + "Nothing waiting since HH:mm" (time of last ack/last alert) + as-of. Must only show "All clear" when the fetch SUCCEEDED; on failure show offline state, never all clear.
- Open items: medium = red-tinted tile (top item text, age, source) + big count; small = count hero red + top item 2 lines + age.
- large: list of open items, severity bar, source, age.
- accessoryCircular (L2): count with "need you" label (green check when zero). Rectangular: "N need you" + top item.

**Merge Queue M1 Pipeline**
- small: "Merge queue" + project; head PR "#485"; four-stage track rebase → gates → push → merge (done green, current accent, rest track); "+N waiting"; last result "#484 ✓"/"✗".
- medium: as small with PR title, stage labels, "#484 merged 2m ago".
- large: pipeline block + FIFO list (position, PR, title, stage) + last 5 results.
- Queue disabled for the project: say "Merge queue off for <project>" (not an empty queue).
- Inline: "⇢ #485 gates · 2 waiting".

**Live Activity A2 Cluster watch**
- Lock screen: header "CLUSTER · <model>" + as-of; four node columns: temp (orange ≥70) + "S1 · util%"; footer "N tok/s · daemon OK · autodown off".
- Dynamic Island compact: leading "● 4/4" (green/orange/red), trailing "72° · 231 t/s"; minimal: hottest temp; expanded: lock-screen layout.
- Started from the app (Cluster screen) — ends after 8 h or when the operator stops it.

## Refresh (card W5f, after the widget audit report)
Proposed: Cluster/Boards/Needs Me 15 min; Running 10 min while cards run, 30 min when idle; Merge Queue 5 min while a PR is in flight, 30 min when empty; app calls `WidgetCenter.reloadAllTimelines()` on foreground and after a successful fetch. Settings cadence must actually drive these. Final values come from `~/fleet-check/opencode-runs/audit-widgets.md`.

## CarPlay (card W5g) — design C "Rows with gauges"

Operator pick 2026-11-20. The visual reference is
`docs/design/2026-11-20-carplay-mock.html` (design C in its DESIGNS array). The
CarPlay scene is part of the iPhone app, not a widget: one `CPTemplateApplicationScene`
(`CPTemplateApplicationSceneSessionRoleApplication` in the app's Info.plist, via
`project.yml`) and no CarPlay widgets.

### The screen

Root is ONE `CPListTemplate`, title "HSCC", five rows, no scrolling at 800×480:

1. **Cluster** — a `CPListImageRowItem` with four node gauges (0–90 °C arc,
   green < 70 / orange 70–74 / red ≥ 75, degrees inside). Each element's title
   is the node short name (◆ for the orchestrator) and its subtitle is
   `34% · 41 t/s`; the row text is `Cluster · 4/4 up · 231 t/s`.
2. **Boards** — stacked bars (running blue / todo grey / blocked orange) for
   the top three boards; detail `2 running · 36 blocked · 89 done today`.
3. **Running** — the longest elapsed time rendered as the image; detail is the
   longest card title.
4. **Needs you** — the unacknowledged count in red, or a green check when the
   fetch succeeded and the list is empty; detail is the top alert title.
5. **Merge queue** — the four-stage rebase/gates/push/merge track; detail
   `#485 in gates · 2 waiting`, or `Merge queue off for <project>` when the
   queue is disabled.

Every row opens a pushed detail `CPListTemplate` (nodes / boards / cards /
alerts / queue rows). Read-only: there is no ack and no write.

### Data, refresh and failure

The scene reads through the same `WidgetDataClient` routes and multi-address
failover the widgets use, then projects with the shared readers
(`ClusterNodeTally`, `ServingSummary`, `NeedsMeReading`, `MergeQueueReading`,
`CardsRunningFeed`). No server route was added.

Refresh every 30 s while the CarPlay scene is connected; the timer is
invalidated on disconnect. The section header carries the data's own clock
(`as of HH:mm`). When data is older than 2× the refresh interval the copy says
`stale · HH:mm`; a failed fetch keeps the last good rows and says
`offline · HH:mm`. **Never "All clear" after a failed fetch** — the Needs row
shows the offline state even when the cached feed was empty.

Images are drawn with `UIGraphicsImageRenderer` at
`templateApplicationScene.carWindow.screen.scale`, using semantic colours
resolved against `CPInterfaceController.carTraitCollection`, so both CarPlay
appearances repaint (a style change re-renders the cached state without a read).

### CarPlay entitlement (simulator only)

Apple has not granted HSCC a CarPlay entitlement, and none was invented. The
`com.apple.developer.carplay-driving-task` key exists ONLY in
`ios-app/Sources/HSCC.simulator.entitlements`, selected by
`CODE_SIGN_ENTITLEMENTS[sdk=iphonesimulator*]` in `project.yml`. Device and
release builds (`scripts/release_apps.sh` → `ios-app/scripts/release_adhoc.sh`,
the Ad Hoc IPA) keep `Sources/HSCC.entitlements` and the two extension
entitlements byte for byte. `ios-app/scripts/carplay_check.sh` pins their
SHA-256, checks the SDK condition and compiles the scene against the iOS SDK
plus executes the row rules on macOS.

### Running it in Xcode's CarPlay simulator

1. `cd ios-app && xcodegen generate` (or open the existing generated project).
2. Run the **HSCC** scheme on an iPhone simulator.
3. In the simulator: **I/O → External Displays → CarPlay**. The car window
   connects and the HSCC scene appears in the CarPlay sidebar.
4. The scene needs the same setup as the widgets: set the cluster address and
   token in the phone app first (Settings / connection screen).
5. `I/O → External Displays → CarPlay` again disconnects; the refresh timer
   stops with the scene.

Do not install this build on a device through a CarPlay-enabled signing path:
only the simulator entitlement file carries the key, and the device build does
not compile a CarPlay-enabled signature.
