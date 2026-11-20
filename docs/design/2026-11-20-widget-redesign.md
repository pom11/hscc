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

- Background: `containerBackground(.fill.tertiary/.background)` system default; no custom paint.
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
