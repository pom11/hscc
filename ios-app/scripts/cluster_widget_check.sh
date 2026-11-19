#!/bin/bash
# cluster_widget_check.sh — prove what the cluster widget shows, by EXECUTING
# the committed projection + text model.
#
# The surface mirrors `hscc cluster monitor` / `hscc cluster status`:
#   * the headline is the CLI host tally (answered / listed), never a state word
#     the host table does not print;
#   * every node row is that host's sample + its serving-table model;
#   * the throughput is the CLI total row's sum of serving[].gen_tps;
#   * the temperatures are the sample's CPU/GPU temps, not a load average the
#     CLI host table does not show;
#   * the old hard-coded `.244/.246/.247/.248` topology glyph is gone — four
#     invented node identities were the biggest "does not match the CLI" bug;
#   * the kanban board row is gone from the cluster tile; it is not cluster
#     monitor output (the board widgets own that data);
#   * the three Lock Screen accessories draw the four numbers the CLI's rows add
#     up to — hottest temperature, GPU load (a MEAN over the hosts that
#     reported), fleet watts (a SUM) and running/waiting — projected from the
#     same view-model by Sources/Shared/ClusterLockScreen.swift, so a Lock
#     Screen temperature is the CLI's own cell and not a second rounding.
#
# Reads are named methods on WidgetDataClient, so the closed GET allowlist stays
# the gate. Structural greps pin the family dispatch and the removal of the
# invented glyph; the macOS harness executes the real source against the
# committed, scrubbed live capture.
#
# Usage: ios-app/scripts/cluster_widget_check.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
SDK=$(xcrun --sdk macosx --show-sdk-path 2>/dev/null) || {
  echo "error: no macOS SDK (is Xcode installed?) " >&2; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

SUBJECT=Sources/HSCCWidgets/ClusterWidget.swift
VIEWS=Sources/HSCCWidgets/ClusterWidgetViews.swift
TABLE_VIEWS=Sources/HSCCWidgets/ClusterMonitorTableViews.swift
METRICS=Sources/HSCCWidgets/ClusterWidgetMetrics.swift
READING=Sources/HSCCWidgets/CLIMonitorTableReading.swift
TABLE=Sources/HSCCWidgets/CLIMonitorHostTableView.swift
MONVIEWS=Sources/HSCCWidgets/MonitorViews.swift
MODEL=Sources/Shared/ClusterMonitorViewModel.swift
TALLY=Sources/Shared/ClusterNodeTally.swift
# The two biggest surfaces (t_9e64d9d9, re-integrated by t_5cc43192). The plan
# and the XL-only fields are Foundation-only and are EXECUTED below; the views
# that draw them are SwiftUI and can only be grepped, so they are two different
# variables and two different kinds of proof.
TABLES=Sources/Shared/ClusterMonitorTables.swift
# The systemSmall tile's data half (t_9e110d88): `ClusterWidgetMetrics.swift`
# (the file this harness compiles) now carries the tile the small family draws, so
# the harness must see that type too. It is Foundation-only, like everything else
# compiled here; the view-model it reads is already `$MODEL`.
GRID=Sources/HSCCWidgets/ClusterSmallGrid.swift
# Grep-only: `MonitorEntry` lives here, and it is SwiftUI, so this gate can read
# it but cannot compile it.
MWIDGET=Sources/HSCCWidgets/MonitorWidget.swift
STORE=Sources/Shared/SharedStore.swift
# The Lock Screen's four numbers + the one copy rule its three families share
# (t_314a3811). Foundation-only like $MODEL above, which is what lets the
# harness execute them: every number here is projected from that view-model.
LOCK=Sources/Shared/ClusterLockScreen.swift
for f in "$SUBJECT" "$VIEWS" "$TABLE_VIEWS" "$METRICS" "$READING" "$TABLE" "$MONVIEWS" \
         "$MODEL" "$TALLY" "$GRID" "$MWIDGET" "$TABLES" "$STORE" "$LOCK" \
         scripts/cluster_widget_check/main.swift; do
  [ -f "$f" ] || { echo "error: $f missing" >&2; exit 1; }
done

rc=0
report() {  # report <label> <grep-output> — non-empty output is a failure
  if [ -n "${2:-}" ]; then
    echo "FAIL: $1"; echo "$2" | sed 's/^/    /'; rc=1
  else
    echo "  ok: $1"
  fi
}

fail_when_absent() {  # fail_when_absent <label> <file> <pattern>
  if grep -q -- "$3" "$2" 2>/dev/null; then
    echo "  ok: $1"
  else
    echo "FAIL: $1  (no '$3' in $2)"; rc=1
  fi
}

echo "structural proofs on the cluster widget file set"

report "no /v1/ route literal in the widget file set (reads go through the client)" \
  "$(grep -n '"/v1/' "$SUBJECT" "$VIEWS" "$METRICS")"

reads=$(grep -c 'WidgetData.read(' "$SUBJECT")
report "exactly 2 WidgetData.read calls (cluster/monitor + cluster/status), got $reads" \
  "$([ "$reads" -eq 2 ] || echo x)"
fail_when_absent "the row read is cluster/monitor" "$SUBJECT" '$0.clusterMonitor()'
fail_when_absent "the job/host read is cluster/status" "$SUBJECT" '$0.clusterStatus()'
report "the cluster tile no longer reads autodown or kanban" \
  "$(grep -n 'autodownStatus\|kanbanRunning\|kanbanBlocked\|kanbanStale' "$SUBJECT")"

report "the invented .244/.246/.247/.248 topology is gone" \
  "$(grep -n '\.244\|\.246\|\.247\|\.248\|canonicalPairs\|TopologyPair' "$SUBJECT" "$VIEWS" "$METRICS")"
# Scoped, and deliberately so. `cluster_live.py`'s host table has no load column,
# so a tile that MIRRORS that table must not invent one — that is the ban. The
# extra-large surface goes one step further than the CLI and prints the route's
# load 1/5/15 in its own detail block, labelled for what it is, on the single
# size with room for it. So the assertion covers the files that mirror the CLI's
# output, and the XL-only fields are asserted as belonging to the shared layout
# rather than being wished out of existence.
report "no load average anywhere the tile mirrors \`cluster monitor\` output" \
  "$(grep -n 'cpuLoad1m\|load1m' "$SUBJECT" "$VIEWS" "$METRICS")"
fail_when_absent "the extra-large-only fields are selected by the shared layout" \
  "$TABLE_VIEWS" 'ClusterMonitorTableLayout.visibleDetails'
fail_when_absent "…and not formatted by the view" \
  "$TABLE_VIEWS" 'CLIMonitorHostRow'

for fam in systemSmall systemMedium systemLarge systemExtraLarge \
           accessoryInline accessoryCircular accessoryRectangular; do
  fail_when_absent "supportedFamilies offers .$fam" "$VIEWS" ".$fam"
done
for fam in systemSmall systemMedium systemLarge systemExtraLarge \
           accessoryInline accessoryCircular accessoryRectangular; do
  fail_when_absent "the view dispatches .$fam" "$VIEWS" "case .$fam:"
done
fail_when_absent "large has its own layout" "$VIEWS" 'struct LargeClusterWidget'
fail_when_absent "extra-large has its own layout, not a re-used large one" \
  "$VIEWS" 'struct ExtraLargeClusterWidget'

fail_when_absent "every family renders the shared text model" "$VIEWS" 'entry.text(for:'
# The small family is the DENSE GRID now (t_9e110d88): up to four hosts x four CLI
# columns plus the serving totals, drawn by ClusterSmallGridView. The old claim
# here was "small is one headline + detail" (a 32pt number), which is exactly the
# shape this card was written to end, so the anchor moved to the grid type. Two
# arms stay asserted: the grid is reached through the entry's ONE gate step (so no
# branch can grid dashes over a paused surface), and the headline the tile shows
# beside it still comes from the shared text model rather than a re-composed
# number. The size is deliberately not pinned — a font size is not a claim about
# honesty, and pinning one is what made this anchor rot.
fail_when_absent "small draws the dense grid" "$VIEWS" 'ClusterSmallGridView(tile:'
fail_when_absent "small reaches the grid through the entry's gate" "$VIEWS" \
  'if let tile = entry.smallTile'
fail_when_absent "small's headline still comes from the shared text model" "$VIEWS" \
  'ClusterHeadline(entry: entry, size:'
fail_when_absent "circular uses the up/total gauge" "$VIEWS" 'Gauge(value:'

fail_when_absent "the projection uses the shared FleetProjection" "$METRICS" 'typealias ClusterFleet = FleetProjection'
fail_when_absent "status jobs come from workloads.count" "$METRICS" 'response.workloads.count'

# ---------------------------------------------------------------------------
# The CLI host table (t_2d1c12dc): `systemMedium` IS `host_table`.
#
# The executed half below proves the cells, the titles, the verdicts and the
# row budget. These structural proofs cover what an executed check cannot see:
# that the two CLI-shaped tiles share the ONE table view, and that no tile has
# quietly re-typed a column name (a transcription is a column that can drift
# from the CLI while every executed assertion still passes).
# ---------------------------------------------------------------------------
echo ""
echo "CLI host-table proofs (the medium tile draws host_table)"

fail_when_absent "the cluster medium draws the shared CLI table" "$VIEWS" 'CLIMonitorHostTableView(rows:'
fail_when_absent "the monitor medium draws the SAME table" "$MONVIEWS" 'CLIMonitorHostTableView(rows:'
fail_when_absent "both entries hand it rows through the gate-checked accessor" "$VIEWS" 'entry.cliHostRows'
fail_when_absent "and so does the monitor entry" "$MONVIEWS" 'entry.cliHostRows'
fail_when_absent "the drawn titles are the CLI's own column list" "$READING" 'CLIMonitorHostRow.columns'
fail_when_absent "the drawn cells are the CLI's own row" "$READING" 'row.cells'
fail_when_absent "the verdicts come from the view-model" "$READING" 'ClusterMetricLevel'

# No CLI title re-typed into a body. A grep for the CLI's own header words in
# the view/metrics files means a second copy of the column list exists, and a
# second copy is a column that drifts.
report "no CLI column title is transcribed into a widget body" \
  "$(grep -n '"CPU Temp"\|"GPU Temp"\|"GPU Power"\|"Swap%"' "$VIEWS" "$MONVIEWS" "$METRICS")"

# A family that reaches `entry.monitor` directly is a family that skipped
# `cliHostRows`, which is the one place unconfigured / paused / "nodes" off are
# refused. Numbers on a paused surface is the t_5c554c5b failure again.
report "no family reaches entry.monitor directly (rows go through cliHostRows)" \
  "$(grep -n 'entry\.monitor' "$VIEWS" "$MONVIEWS")"

# The reading layer stays Foundation-only so the checks below can compile and RUN
# it. The comment lines are excluded — the file's own header explains why it must
# not name a Colour, and prose is not a colour (same rule the paused-copy count
# learned on #341).
report "the reading layer names no SwiftUI and no Colour, so it is executed below" \
  "$(grep -n 'import SwiftUI\|Color' "$READING" | grep -v '^[0-9]*:[[:space:]]*//')"

# The tile draws cells; it does not write any. Every `Text` in it takes a value
# from the reading layer or the view-model.
report "the table view composes no cell text of its own" \
  "$(grep -n 'Text(\s*"' "$TABLE")"

# ---------------------------------------------------------------------------
# One host-table stack (t_5cc43192).
#
# #409 grew a SECOND host-table model beside the one #408 had just landed — its
# own column enums, its own visible-row count, its own host rows inside
# `ClusterMonitorTables`. Re-integrating onto main means main's stack wins, and
# the shape of the bug is one that a compile cannot catch: two column enums that
# agree today drift the first time the CLI renames a header. So the assertions
# here are the negative ones — the duplicate must STAY deleted — plus the one
# positive claim that survives the merge: the big surfaces add a serving table
# and a detail block, and take their host rows from main's view.
# ---------------------------------------------------------------------------
echo ""
echo "one host-table stack proofs (large/XL add blocks, not a second table)"

# Each deleted name needs a guard in front of it, because main's OWN names
# contain them as suffixes: `CLIMonitorHostTableView` ends in
# `MonitorHostTableView`, `CLIMonitorHostColumn` ends in `MonitorHostColumn`, and
# `hiddenRowCount` is main's own function in CLIMonitorTableReading.swift. A bare
# substring grep for the deleted names therefore reported "the duplicate is
# back" while pointing at main's code — and, worse, a mutation that actually
# reintroduced the duplicate could not be told apart from that permanent noise.
# The trailing class keeps `MonitorHostColumn` from matching inside
# `ClusterMonitorHostColumn` the same way the leading class keeps it from
# matching inside `CLIMonitorHostColumn`.
report "the duplicate host-table stack stayed deleted" \
  "$(grep -rnE '(^|[^A-Za-z])(MonitorHostTableView|MonitorHostColumn|ClusterMonitorHostColumn|visibleHostRows)([^A-Za-z]|$)' \
        Sources/HSCCWidgets/ Sources/Shared/ClusterMonitorTables.swift)"

# Host rows on large/XL are main's table view, given a taller budget. `rowLimit:`
# is what makes this a budget rather than a second clip function: the plan owns
# the number, main's view owns the `+n`. There is exactly ONE call site with a
# rowLimit on it, in the shared block — a second one would be the duplicate
# stack's ghost.
fail_when_absent "the big surfaces draw main's table view at a plan-sized budget" \
  "$TABLE_VIEWS" 'CLIMonitorHostTableView(rows: hostRows, rowLimit: plan.hostRowLimit)'
report "no second call site gives the host table its own row budget" \
  "$(grep -rn 'CLIMonitorHostTableView(' Sources/HSCCWidgets/ \
        | grep -v 'CLIMonitorHostTableView(rows: rows)' \
        | grep -v 'CLIMonitorHostTableView(rows: hostRows, rowLimit: plan.hostRowLimit)' \
        | grep -v 'struct CLIMonitorHostTableView')"
# Both tiles route large/XL through the ONE shared block, so neither size has a
# host table of its own — and the claim is a COUNT, not an existence. An earlier
# revision of these two lines asked only "does this file mention the block?",
# which a mutation broke by renaming ONE of a tile's two call sites: the other
# still matched, and the gate stayed green while one of the two biggest surfaces
# had silently stopped drawing the tables.
for pair in "$VIEWS:cluster" "$MONVIEWS:monitor"; do
  f="${pair%%:*}"; who="${pair##*:}"
  blocks=$(grep -c 'MonitorTablesBlock(tables:' "$f")
  report "the $who tile routes exactly large + extra-large through the shared block (got $blocks)" \
    "$([ "$blocks" -eq 2 ] || echo x)"
done
fail_when_absent "…and a body asks the entry before drawing one" "$VIEWS" \
  'if let tables = entry.tables'
fail_when_absent "…and so does the monitor tile" "$MONVIEWS" \
  'if let tables = entry.tables'
report "no file outside main's reading layer counts visible host rows" \
  "$(grep -rn 'visibleHostRows' Sources/HSCCWidgets/ Sources/Shared/ClusterMonitorTables.swift)"

# The serving table is this card's only non-overlap, so it is asserted as
# main's rows. Anchored on the DECLARATION (`let rows: [CLIMonitorServingRow]`),
# not on the type name appearing anywhere: an earlier revision accepted any
# mention, and the file's own header comment mentions it — so a mutation that
# renamed the real parameter while leaving the comment alone read green.
fail_when_absent "the serving table's rows are main's row type" "$TABLE_VIEWS" \
  'let rows: \[CLIMonitorServingRow\]'
report "the serving table re-declares no row or total type of its own" \
  "$(grep -n 'struct .*ServingRow\|struct .*ServingTotal' "$TABLE_VIEWS" \
        Sources/Shared/ClusterMonitorTables.swift)"
fail_when_absent "the serving table is drawn by the shared block, once" "$TABLE_VIEWS" \
  'MonitorServingTableView('
fail_when_absent "the XL detail block is asked for only where the plan says so" \
  "$TABLE_VIEWS" 'if plan.showsHostDetail {'
fail_when_absent "the XL-only fields are selected by the shared layout" "$TABLES" \
  'static func visibleDetails'
report "…and not formatted by the view" \
  "$(grep -n 'vramUsed\|gpuClock\|netRecv\|uptimeSeconds\|fanPercent\|load1m' "$TABLE_VIEWS")"
# The detail block dashes with the CLI's hyphen (`CLIMonitorFormat.dash`), the
# glyph the host table one spacing value above it prints. `FleetFormat.dash` is
# an em dash and belongs to the fleet SENTENCES; one table wearing both is two
# kinds of absence that read as one. Comment lines are excluded — this file's own
# header explains the choice and names the rejected constant, and prose is not a
# colour or a glyph (the same rule the reading-layer check below learned on #341).
report "the detail block dashes like the table above it, not like the prose" \
  "$(grep -n 'FleetFormat' "$TABLES" | grep -v '^[0-9]*:[[:space:]]*//')"

# One level->palette mapping for the CLI-shaped tables. A third `case .ok:` in
# the widget target is the same drift risk in colour form; the dense small tile
# is allowed its OWN rule and names it as such (`color(_:)` on the glance grid,
# `cliCellColor(_:)` for the CLI cells — two named rules, never two unnamed ones).
# The claim is an OCCURRENCE count and not a file count: a mutation that adds a
# second switch inside a file that is already allowed slips past a file-count
# check entirely, and "two allowed files" is not the invariant — "one rule per
# surface family" is.
occurrences=$(grep -rn 'case \.ok: return style' Sources/HSCCWidgets/ | wc -l | tr -d ' ')
report "exactly two level->palette switches exist in the widget target (got $occurrences)" \
  "$([ "$occurrences" -eq 2 ] || echo x)"
report "…and they are the tables' and the small tile's, not some third file" \
  "$(grep -rl 'case \.ok: return style' Sources/HSCCWidgets/ | sort | grep -v \
        -e 'Sources/HSCCWidgets/ClusterMonitorTableViews.swift' \
        -e 'Sources/HSCCWidgets/ClusterSmallGridView.swift')"
# The CLI host table does not keep its own mapping any more: it delegates. Without
# this line the count above stays at two whether the table shares the rule or
# quietly grew its own third one, because `ink` would then be the third switch and
# the file-count check would name it — but only after a human read it.
fail_when_absent "the CLI host table's ink IS the shared rule, not a private copy" \
  "$TABLE" 'cliCellColor(style)'

# One "may this surface show numbers at all" per entry, and both entries answer
# it with the same predicate `cliHostRows` implements — the second definition is
# what let a paused tile keep its numbers once already.
fail_when_absent "the cluster entry owns the table gate over cliHostRows" "$METRICS" \
  'guard cliHostRows != nil else { return nil }'
fail_when_absent "and the monitor entry gates the same way" "$MWIDGET" \
  'guard cliHostRows != nil else { return nil }'

# §7: the workload projection may name ids only — no recipe path, image or
# container name may even be decoded.
wl=$(sed -n '/^struct MonitorWorkload/,/^}/p' Sources/Shared/SharedModels.swift)
report "MonitorWorkload decodes only ids (no recipe/containers/image)" \
  "$(printf '%s\n' "$wl" | grep -n 'recipe\|containers\|image')"
for key in workload_id cluster_id; do
  report "$key is declared without a default (a default would skip decoding)" \
    "$(printf '%s\n' "$wl" | grep -n "let $key:.*= nil")"
done

echo ""
echo "executed proofs (macOS compile of the real widget + shared sources)"

xcrun swiftc -sdk "$SDK" -o "$TMP/cluster_widget_check" \
  "$METRICS" \
  "$TALLY" \
  "$MODEL" \
  "$TABLES" \
  "$READING" \
  "$GRID" \
  "$LOCK" \
  Sources/Shared/SharedModels.swift Sources/Shared/SharedStore.swift \
  Sources/Shared/WidgetDataClient.swift \
  scripts/model_decode_check/ThemeStub.swift \
  scripts/cluster_widget_check/main.swift \
  2> "$TMP/compile.err"
if [ -s "$TMP/compile.err" ]; then
  echo "FAIL: harness compile produced diagnostics:"
  sed 's/^/    /' "$TMP/compile.err"
  exit 1
fi
"$TMP/cluster_widget_check" || rc=1

echo ""
if [ "$rc" -eq 0 ]; then
  echo "CLUSTER WIDGET CHECK PASSED — CLI rows, every family's lines, nil-is-not-zero, redaction, one host-table stack"
  exit 0
fi
exit 1
