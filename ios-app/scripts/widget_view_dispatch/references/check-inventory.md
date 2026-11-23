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

## Surface added after the split — `widgetClusterGraph` → `widget_view_dispatch/widgetClusterGraph.sh`

`t_e0e324da` registers the Cluster Graph widget (t_f4551339). The tables above are the
`c270ba84` baseline and were left alone: this entry extends the inventory rather than
re-totalling it.

| id | asserts (`ok:` text verbatim where shared) |
|---|---|
| `graph-3a` | `widget_gate_families <provider> home-screen-only` → `supportedFamilies are Home Screen sizes only — no complication body that could need a label`. **First caller of this mode in the manifest** — every shipped surface has since gained an accessory family, so the branch had no caller at all until now. The mode does two things and they reach differently on THIS widget. The label half reads the provider normally. The accessory half greps a `supportedFamilies([...])` **literal** for `.accessory`, and `3852bbf6` left this file with no literal at all — it passes the named `WidgetFamily.clusterGraphFamilies` — so that half cannot match here and its `ok` measures nothing. It is therefore NOT the evidence that this widget has no accessory family; `graph-own-3` is. What is proven is that the half still has teeth when a literal exists: planting `[.systemSmall, .accessoryInline]` reddens it by name (`L14`, `cluster_graph_check/negative_legs.sh`). This row formerly named the `#else` macOS literal arm as its mutation target; that arm no longer exists, so the row no longer points at text that is not in the file. |
| `graph-3b×4` | `widget_gate_no_short_gate_label` on each of the four files the widget ships (provider, entry, views, metrics) → `no shortGateLabel in a widget with no accessory family (the absence is proven, not assumed)`. Proving the absence per file, not once for the widget, is what catches a label added later to the entry or to a view. Negative-proven on the entry and on a view. |
| `graph-4` | `widget_gate_registry_key <provider> widgetClusterGraph` → `cadence and pause come from WidgetSurfaceKind.widgetClusterGraph (one registry, one write path)`. New coverage: `cluster_graph_check.sh` proves the surface HAS a settings key in `SharedStore`; nothing proved the widget READS it, and a widget can hold a key it never consults and ignore a pause someone can see in Settings. |
| `graph-5` | `widget_gate_paused_factory <provider> "status: .paused"` → `the provider returns the entry's own paused factory (status: .paused)`. There is no `.paused(...)` factory here and none was invented to satisfy the helper: `ClusterGraphEntry.status` is one value, so naming the paused case IS this entry's way of saying paused. |
| `graph-2` | `widget_gate_one_paused_copy <entry>` → `one paused copy, owned by the entry (gateLine)`. Measured on the entry's file, not the provider's — the reason gate (2) is callable on its own now. |
| `graph-7` + `graph-8a..d` | `widget_gate_cadence <provider> cluster_graph_cadence` — END-ANCHORED marker pair, slice contains `nextRefreshDate`, then the four executed cadence checks. Negative-proven twice: renaming the marker fails it by name, and making the `Off` branch return a date fails the executed table (that mutation also proves a child's non-zero exit is not swallowed by the aggregator — aggregate rc=1 with no green banner). |
| `graph-own-1` | `widget_gate_rule_has_owner scripts/cluster_graph_check.sh 'if !entry.configured \|\| entry.paused {'` → `… is owned by scripts/cluster_graph_check.sh (delegation verified here, not duplicated)`. Replaces the helper's gate (1), which is NOT measured here. |
| `graph-own-2` | `widget_gate_rule_has_owner scripts/cluster_graph_check.sh '[ "$pause_at" -lt "$read_at" ]' …`. Replaces the helper's gate (6). This widget ships INVITE before PAUSE deliberately — `status` is single-valued, so `configured` and `paused` cannot both be true and whoever asks first decides the sentence the operator reads; asking paused first would let a device that lost its token while the surface was off print "Paused in Settings" forever. What the fleet ordering protects (a paused surface issues zero requests) is what the owner measures, and it holds either way round. |
| `graph-own-3` | `widget_gate_rule_has_owner scripts/cluster_graph_check.sh '\.accessory'` → `no accessory family anywhere in the graph widget (home-screen-only) is owned by scripts/cluster_graph_check.sh (delegation verified here, not duplicated)`. Added by t_4311500c because `graph-3a`'s accessory half is vacuous on a named list: the claim that actually holds — no accessory family anywhere, across all four shipped files and the shared list wherever it is defined — has exactly ONE owner, `cluster_graph_check.sh` §2, and this row is what stops that owner being edited away in silence. Negative-proven by `L10`: delete the rule from the owner and this file reddens, naming the file that has to hold it. |

**Checks: 15** (11 structural + 4 executed cadence), run rc=0 — measured by t_4311500c at its branch head, and the authority for both figures is the run rather than this sentence: `bash ios-app/scripts/widget_view_dispatch/widgetClusterGraph.sh | grep -c '  ok: '` prints the surface's number, `bash ios-app/scripts/widget_view_dispatch_check.sh | grep -c '  ok: '` the aggregate's (that run printed 114 stdout lines / 93 `ok:` / 0 `FAIL:`, and its diff against the `c270ba84` baseline for the same tree is additive only — the six shipped surfaces print byte-identical output).

The figures above are point-in-time evidence for the commit that wrote them, not a contract. A count transcribed into a doc rots on the next neighbour who adds a check — this repo has shipped one that was stale in the commit that wrote it, and `widget_settings_check.sh` shipped an aggregate that reddened a healthy gate over a number. The inventory's job is the assertions and their owners; the tally belongs to the run.

All three delegations above are two-sided: deleting any of them from
`cluster_graph_check.sh` reddens this surface file, and an accessory family added to the
SHARED `clusterGraphFamilies` list reddens `cluster_graph_check.sh` while this file stays
green — which is the test that the ownership split is real rather than declared.

## Not touched by this card

`widget_settings_check.sh` (same append-only smell, separate card), `merge_base_check.sh`,
`sf_symbol_check.sh`, `projects_widget_check.sh`, `board_status_widget_check/dispatch_order.py`,
and every file under `Sources/`.
