# `widget_view_dispatch_check.sh` — check inventory at `c270ba84`

The contract this decomposition must preserve. Every entry below is one check the harness
prints a verdict for; the split moves checks between files and may not weaken, drop, merge or
simplify any of them.

**Every count in this file is a point-in-time measurement at `c270ba84`, not a figure to keep
in sync.** The harness prints its own verdict lines and fails on a miss; nothing reads these
numbers. A later card that adds a check makes the totals here historical, and that is correct —
do not "update" them, extend the inventory with the new check's entry.

Subject of measurement for every entry: the committed Swift under `ios-app/Sources/`. Nothing
here renders pixels (no iOS runtime on this host) — the greps prove structure, the compiled
blocks prove logic.

## Baseline measured at `c270ba84fc1c8077a331c536ff54d09d54504ed3`

`./ios-app/scripts/widget_view_dispatch_check.sh` → **rc=0**, 60 stdout lines, 0 stderr lines:

| kind | count |
|---|---|
| `  ok:` verdict lines | 43 |
| `FAIL:` verdict lines | 0 |
| `state=… configured=… -> …` dispatch-pair lines | 10 |
| `source 'if !entry.configured' occurrences = …` line | 1 |
| `RESULT:` section banners | 5 |
| final banner (`WIDGET SURFACE GATE CHECK PASSED …`) | 1 |

**Total checks = 54** (43 `ok:` + 10 dispatch pairs + 1 source-order grep), per the per-surface
table at the bottom.

## Line ranges in the pre-split file

`widget_view_dispatch_check.sh` @ `c270ba84` = 386 lines, seven regions:

| region | lines | destination |
|---|---|---|
| A header + `set -uo pipefail` + `cd` | 1–12 | aggregator (header) + `common.sh` (`cd`, `SDK`) |
| B cluster widget section | 14–50 | `widget_view_dispatch/widgetCluster.sh` |
| C board status widget section (hand-written, #340) | 52–148 | `widget_view_dispatch/widgetBoardStatus.sh` |
| D `widget_surface_gate` + `wok`/`wbad`/`wgfail` | 150–342 | `widget_view_dispatch/common.sh` (shared, NOT duplicated) |
| E kanban monitor call | 344–350 | `widget_view_dispatch/widgetKanbanMonitor.sh` |
| F running cards call | 357–363 | `widget_view_dispatch/widgetCardsRunning.sh` |
| G projects call + tail banners + `exit $rc` | 365–386 | `widget_view_dispatch/widgetProjects.sh` + aggregator tail |

## Surface 1 — `WidgetSurfaceKind.widgetCluster` → `widgetCluster.sh`

Subject: `Sources/HSCCWidgets/ClusterWidgetViews.swift`. Not registry-driven (predates the
registry, t_5c554c5b), so this surface has no `setting(for:)` / cadence check.

| id | lines | asserts |
|---|---|---|
| `cluster-1` | 14–17 | `grep -c 'if !entry.configured'` on the subject `>= 2` (both bodies check unconfigured before unreachable). Prints `source 'if !entry.configured' occurrences = $count (want 2: small+medium bodies)`. **Hard `exit 1`** with `❌ source order regressed: unconfigured no longer checked first`. |
| `cluster-2` | 19–50 | compiled dispatch mirror: `enum S` (serving/waking/down/unreachable/unknown) × `configured` ∈ {false,true} → 10 pairs through a transcription of the committed `dispatchView` chain; every `configured=false` pair must yield `unconfiguredView`. Prints one `state=… configured=… -> …` line per pair (10) + `RESULT: OK - every unconfigured entry invites setup`, or `RESULT: FAIL - some unconfigured entry shows a non-setup view` + `exit(1)`. Its exit status is the value the script finally `exit`s with (`rc`). |
| (guard) | 46 | `xcrun --sdk macosx --show-sdk-path` failure → `no macOS SDK` on stderr + `exit 1`. Now owned by `common.sh`, which resolves `SDK` once; the aggregator refuses to run any surface without it. |
| (guard) | 47 | `swiftc` compile failure of the mirror → `compile failed` on stderr + `exit 1`. |

**Checks: 11** (1 source-order grep + 10 dispatch pairs).

## Surface 2 — `WidgetSurfaceKind.widgetBoardStatus` → `widgetBoardStatus.sh`

Subject: `Sources/HSCCWidgets/BoardStatusWidget.swift`. This section is the hand-written shape
#340 shipped; the card forbids converting it into a `widget_surface_gate` call, and this split
does not — it moves it verbatim.

| id | lines | asserts (`ok:` text verbatim) |
|---|---|---|
| (guard) | 61–62 | `[ -f "$BOARD" ]` else `missing $BOARD` on stderr + `exit 1`. |
| `board-1` | 69–70 | `grep -c 'if !entry.configured \|\| entry.paused' >= 5` → `5 bodies (rectangular, small, medium, large + circular) gate on !configured\|\|paused`; else `FAIL: only $gate bodies gate on …`. |
| `board-2` | 71–73 | exactly one `if !configured \|\| paused { return shortGateLabel }` → `the inline family routes both gate states through the same copy rule`. |
| `board-3` | 74–76 | `grep -q 'paused ? "Paused in Settings"'` → `one paused copy, owned by the entry (gateLine)`. |
| `board-4` | 77–93 | inside the awk-located `private func entry(for configuration:` region, line of `if let surface, !surface.isActive` **<** line of `WidgetDataClient.makeIfConfigured()` → `inside entry(): paused is decided (line N) before the client exists (line M) — a paused surface sends zero requests`. Empty region is itself a FAIL (the order claim would silently stop being measured). |
| `board-5` | 94–96 | `grep -q 'return .paused(boardSlug: boardSlug)'` → `the paused entry is built by the entry's own factory (no projection, no failure)`. |
| `board-6` | 97–100 | `grep -q 'setting(for: .widgetBoardStatus)'` → `cadence and pause come from WidgetSurfaceKind.widgetBoardStatus (one registry, one write path)`. |
| `board-7` | 102–116 | END-ANCHORED `// GATE-SLICE-BEGIN: board_status_cadence[[:space:]]*$` / matching END found (missing → FAIL **by name**), and the slice contains `func nextRefreshDate`. |
| `board-8a..d` | 117–148 | slice spliced into `enum BoardStatusTimelineProvider` + compiled + **executed**: `Off cadence yields no next date (policy .atEnd)`, `negative cadence yields no next date (no refresh loop)`, `5 min cadence is exactly +300s`, `60 min cadence is exactly +3600s`, then `RESULT: OK - board status cadence: Off and negative mean atEnd, real values mean minutes`. |

**Checks: 10** (6 structural + 4 executed cadence).

## Surface 3 — `WidgetSurfaceKind.widgetKanbanMonitor` → `widgetKanbanMonitor.sh`

One `widget_surface_gate` call (344–350) with
`file=Sources/HSCCWidgets/KanbanMonitorWidget.swift`, `slice=kanban_monitor_cadence`,
`factory=".paused(refreshMinutes:"`, `want=6`,
`region_re='/private func fetchEntry\(surface:/ {f=1} f {print} f && /^    \}$/ {exit}'`,
`key=widgetKanbanMonitor`, families default = `accessory`.

## Surface 4 — `WidgetSurfaceKind.widgetCardsRunning` → `widgetCardsRunning.sh`

One call (357–363) with `file=Sources/HSCCWidgets/CardsRunningWidget.swift`,
`slice=cards_running_cadence`, `factory="return .paused()"`, `want=5`,
`region_re='/private func entry\(surface:/ {f=1} f {print} f && /^    \}$/ {exit}'`,
`key=widgetCardsRunning`, families = `accessory`.

## Surface 5 — `WidgetSurfaceKind.widgetProjects` → `widgetProjects.sh`

One call (371–378) with `file=Sources/HSCCWidgets/ProjectsWidget.swift`,
`slice=projects_cadence`, `factory=".paused(selectedName:"`, `want=3`,
`region_re='/private func fetch\(for configuration:/ {f=1} f {print} f && /^    \}$/ {exit}'`,
`key=widgetProjects`, families = **`home-screen-only`** (this widget has no accessory family, so
checks (3)/(3b) assert the INVERSE).

### The expanded body of `widget_surface_gate` (150–342) — what each of the three calls above runs

| id | lines | asserts |
|---|---|---|
| `gate-0` | 183 | `[ -f "$file" ]` else `FAIL: missing $file` + return. |
| `gate-1` | 186–192 | `grep -c 'if !entry.configured \|\| entry.paused' >= want` → `N bodies gate on !configured\|\|paused (want >= W)`. |
| `gate-2` | 194–204 | `grep '"Paused in Settings"'` minus comment lines == exactly 1 → `one paused copy, owned by the entry (gateLine)`. |
| `gate-3` (accessory) | 227–241 | `grep -E 'Text\(entry\.shortGateLabel\)\|return shortGateLabel'` minus comments `>= 2` → `accessory families draw shortGateLabel (N sites)`. |
| `gate-3-alt` (home-screen-only) | 213–226 | `supportedFamilies` names NO `.accessory*` → `supportedFamilies are Home Screen sizes only — no complication body that could need a label`; AND no non-comment `shortGateLabel` → `no shortGateLabel in a widget with no accessory family (the absence is proven, not assumed)`. Two checks, both the inverse of the accessory pair. |
| `gate-3b` (accessory) | 243–258 | inside `var shortGateLabel: String {`, line of `!configured` **<** line of `Paused` → `shortGateLabel checks !configured (line U) before any paused copy (line P) — an unconfigured device cannot read as paused`. Empty region is itself a FAIL. |
| `gate-4` | 261–266 | `grep -qF "setting(for: .$key)"` → `cadence and pause come from WidgetSurfaceKind.$key (one registry, one write path)`. |
| `gate-5` | 268–274 | `grep -qF "$factory"` → `the provider returns the entry's own paused factory ($factory)`. |
| `gate-6` | 276–292 | inside the awk region named by `region_re`, line of `if let surface, !surface.isActive` **<** line of `WidgetDataClient.makeIfConfigured()` → `inside the fetch: paused decided (line A) before the client exists (line C) — zero requests when paused`. Empty region is itself a FAIL. |
| `gate-7` | 294–307 | END-ANCHORED `GATE-SLICE-BEGIN/END: ${slice}` pair found (missing → FAIL by name **and `return`**, so a renamed marker cannot leave the table green), slice contains `func nextRefreshDate`. |
| `gate-8a..d` | 308–341 | slice spliced into `enum CadenceHost` + compiled + **executed**: `cadence Off cadence yields no next date (policy .atEnd)`, `cadence negative cadence yields no next date (no refresh loop)`, `cadence 5 min cadence is exactly +300s`, `cadence 60 min cadence is exactly +3600s`, then `RESULT: OK - cadence: Off and negative mean atEnd, real values mean minutes`. |

Expanded check counts: `accessory` mode = 7 + 4 cadence = **11**; `home-screen-only` mode =
7 (gate-3 contributes two inverse checks, gate-3b does not run) + 4 = **11**.

## Post-split check table the aggregator must reproduce

| surface | file | checks |
|---|---|---|
| `widgetCluster` | `widget_view_dispatch/widgetCluster.sh` | 11 |
| `widgetBoardStatus` | `widget_view_dispatch/widgetBoardStatus.sh` | 10 |
| `widgetKanbanMonitor` | `widget_view_dispatch/widgetKanbanMonitor.sh` | 11 |
| `widgetCardsRunning` | `widget_view_dispatch/widgetCardsRunning.sh` | 11 |
| `widgetProjects` | `widget_view_dispatch/widgetProjects.sh` | 11 |
| **total** | | **54** |

`FAIL` on the baseline run: **0**. The equivalence proof is byte-identical stdout between the
pre-split and post-split aggregate run.

## Deliberate behaviour deltas (documented, not silent)

1. **A hard failure in one surface no longer aborts the remaining surfaces.** Pre-split,
   `cluster-1`'s regression and the missing-SDK / compile-failure guards `exit 1` from inside
   the script, so the board and registry surfaces never ran. Post-split each surface is a child
   process of the aggregator, so its `exit 1` ends only itself; the aggregator still exits
   non-zero. Strictly more diagnostic; the exit code is unchanged in kind.
2. **The final banner now reflects the aggregate.** Pre-split, a board-status `FAIL` set `rc=1`
   but the widget section still printed `WIDGET SURFACE GATE CHECK PASSED` and only then
   `exit $rc` — a red check printing a green banner. Post-split any failing surface prints
   `WIDGET SURFACE GATE CHECK FAILED — …` and exits 1. On a fully green run the output is
   byte-identical to the baseline.

## Not touched by this card

`widget_settings_check.sh` (same append-only smell, separate card), `merge_base_check.sh`,
`sf_symbol_check.sh`, `projects_widget_check.sh`, `board_status_widget_check/dispatch_order.py`,
and every file under `Sources/`.
