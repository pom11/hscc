#!/bin/bash
# cluster_widget_check.sh — prove what the Cluster C1 widget shows, by
# EXECUTING the committed projection + text model.
#
# The 2026-11-20 operator pick is C1 Health line:
#   small  — one state word ("All up" / "3/4 up" / "Down"), tok/s, hottest temp;
#   medium — the tally column + one util-meter row per node;
#   large  — now tok/s, the 2 h total-throughput area chart, the node table
#            (util, temp, power, tok/s) and the models/daemon footer.
#
# The large tile pays for GET /v1/cluster/history (gen_tps engine rows) and
# GET /v1/daemon/host; every other family pays for the monitor read alone.
# No network, simulator or device: the macOS harness compiles the real sources
# against the committed fixture.
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
KIT=Sources/HSCCWidgets/NativeStyleKit.swift
METRICS=Sources/HSCCWidgets/ClusterWidgetMetrics.swift
MODEL=Sources/Shared/ClusterMonitorViewModel.swift
TALLY=Sources/Shared/ClusterNodeTally.swift
GRID=Sources/HSCCWidgets/ClusterSmallGrid.swift
HIST=Sources/Shared/ClusterHistorySeries.swift
STORE=Sources/Shared/SharedStore.swift
LOCK=Sources/Shared/ClusterLockScreen.swift
NEEDS=Sources/Shared/NeedsMeReading.swift
MERGE=Sources/Shared/MergeQueueReading.swift
READING=Sources/HSCCWidgets/CLIMonitorTableReading.swift
for f in "$SUBJECT" "$VIEWS" "$KIT" "$METRICS" "$MODEL" "$TALLY" "$GRID" "$HIST" \
         "$STORE" "$LOCK" "$NEEDS" "$MERGE" "$READING" \
         Sources/Shared/ClusterMonitorTables.swift \
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
  if grep -qF -- "$3" "$2" 2>/dev/null; then
    echo "  ok: $1"
  else
    echo "FAIL: $1  (no '$3' in $2)"; rc=1
  fi
}

echo "structural proofs on the Cluster C1 file set"

report "no /v1/ route literal in the widget file set (reads go through the client)" \
  "$(grep -n '"/v1/' "$SUBJECT" "$VIEWS" "$METRICS")"

reads=$(grep -c 'WidgetData.read(' "$SUBJECT")
report "3 WidgetData.read sites (monitor + gated history + gated daemon), got $reads" \
  "$([ "$reads" -eq 3 ] || echo x)"
fail_when_absent "the row read is cluster/monitor" "$SUBJECT" '$0.clusterMonitor()'
fail_when_absent "the large tile's chart read is cluster/history" "$SUBJECT" '$0.clusterHistory(window: "6h"'
fail_when_absent "the large tile's footer read is daemon/host" "$SUBJECT" '$0.daemonHost()'
fail_when_absent "the history read is family-gated" "$SUBJECT" \
  'Self.optionalHistory(client: client, enabled: wantsHistory)'
fail_when_absent "the daemon read is family-gated" "$SUBJECT" \
  'Self.optionalDaemonHost(client: client, enabled: wantsHistory)'
skips=$(grep -c 'guard enabled else { return nil }' "$SUBJECT")
report "both optional reads skip the request entirely when disabled, got $skips (want 2)" \
  "$([ "$skips" -eq 2 ] || echo x)"
fail_when_absent "the chart series sums the engine gen_tps rows" "$METRICS" \
  'for: .genTps'
fail_when_absent "the chart projection sums by timestamp" "$METRICS" \
  'sums[sample.t, default: 0] += value'
fail_when_absent "the chart keeps the last 2 h of the 6 h answer" "$METRICS" \
  'last - 2 * 60 * 60'

for fam in systemSmall systemMedium systemLarge systemExtraLarge \
           accessoryInline accessoryCircular accessoryRectangular; do
  fail_when_absent "supportedFamilies offers .$fam" "$VIEWS" ".$fam"
done
for fam in systemSmall systemMedium systemLarge systemExtraLarge \
           accessoryInline accessoryCircular accessoryRectangular; do
  fail_when_absent "the view dispatches .$fam" "$VIEWS" "case .$fam:"
done
fail_when_absent "small is the C1 state line" "$VIEWS" 'Text(c1.headline)'
fail_when_absent "medium draws a util meter per node" "$VIEWS" 'MeterBar(value: node.gpuPct'
fail_when_absent "large draws the 2 h chart" "$VIEWS" 'ClusterThroughputChart(points: entry.throughput)'
fail_when_absent "large draws the node table" "$VIEWS" 'Grid(alignment: .leading'
fail_when_absent "large footer names the models" "$VIEWS" 'Text(c1.modelLine)'
fail_when_absent "large footer names the daemon state" "$VIEWS" 'Text(c1.daemonText)'
fail_when_absent "every Home Screen family takes the unconfigured/paused gate" "$VIEWS" \
  'if !entry.configured || entry.paused'
fail_when_absent "the shared kit owns AsOfStamp" "$KIT" 'struct AsOfStamp'
fail_when_absent "the shared kit owns MeterBar" "$KIT" 'struct MeterBar'
fail_when_absent "the shared kit owns Tile" "$KIT" 'struct Tile'
fail_when_absent "the shared kit owns the stale/offline rule" "$KIT" 'struct StaleModifier'
fail_when_absent "temperature bands are 70/75" "$KIT" 'celsius >= 75'
fail_when_absent "utilisation warns above 85" "$KIT" 'percent > 85'

report "the C1 bodies draw no removed table view" \
  "$(grep -n 'MonitorTablesBlock\|CLIMonitorHostTableView' "$VIEWS")"
report "the invented .244/.246/.247/.248 topology is gone" \
  "$(grep -n '\.244\|\.246\|\.247\|\.248\|canonicalPairs\|TopologyPair' "$SUBJECT" "$VIEWS" "$METRICS")"
report "the cluster tile reads no autodown or kanban data" \
  "$(grep -n 'autodownStatus\|kanbanRunning\|kanbanBlocked\|kanbanStale' "$SUBJECT")"

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
  "$HIST" \
  "$LOCK" \
  "$NEEDS" \
  "$MERGE" \
  Sources/Shared/SharedModels.swift Sources/Shared/SharedStore.swift \
  Sources/Shared/WidgetSurfaceKind.generated.swift \
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
  echo "CLUSTER WIDGET CHECK PASSED — C1 lines, native kit, nil-is-not-zero, redaction"
  exit 0
fi
echo "CLUSTER WIDGET CHECK FAILED — a structural or executed check above is red"
exit 1
