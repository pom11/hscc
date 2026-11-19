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
#     monitor output (the board widgets own that data).
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
METRICS=Sources/HSCCWidgets/ClusterWidgetMetrics.swift
READING=Sources/HSCCWidgets/CLIMonitorTableReading.swift
TABLE=Sources/HSCCWidgets/CLIMonitorHostTableView.swift
MONVIEWS=Sources/HSCCWidgets/MonitorViews.swift
MODEL=Sources/Shared/ClusterMonitorViewModel.swift
TALLY=Sources/Shared/ClusterNodeTally.swift
# The systemSmall tile's data half (t_9e110d88): `ClusterWidgetMetrics.swift`
# (the file this harness compiles) now carries the tile the small family draws, so
# the harness must see that type too. It is Foundation-only, like everything else
# compiled here; the view-model it reads is already `$MODEL`.
GRID=Sources/HSCCWidgets/ClusterSmallGrid.swift
STORE=Sources/Shared/SharedStore.swift
for f in "$SUBJECT" "$VIEWS" "$METRICS" "$READING" "$TABLE" "$MONVIEWS" "$MODEL" \
         "$TALLY" "$GRID" "$STORE" \
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
report "the load average the CLI host table does not print is gone" \
  "$(grep -n 'cpuLoad1m\|load1m' "$SUBJECT" "$VIEWS" "$METRICS")"

for fam in systemSmall systemMedium systemLarge accessoryInline accessoryCircular accessoryRectangular; do
  fail_when_absent "supportedFamilies offers .$fam" "$VIEWS" ".$fam"
done
for fam in systemSmall systemMedium systemLarge accessoryInline accessoryCircular accessoryRectangular; do
  fail_when_absent "the view dispatches .$fam" "$VIEWS" "case .$fam:"
done
fail_when_absent "large has its own layout" "$VIEWS" 'struct LargeClusterWidget'

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
  "$READING" \
  "$GRID" \
  Sources/Shared/ClusterMonitorTables.swift \
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
  echo "CLUSTER WIDGET CHECK PASSED — CLI rows, every family's lines, nil-is-not-zero, redaction"
  exit 0
fi
exit 1
