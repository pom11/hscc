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
TALLY=Sources/Shared/ClusterNodeTally.swift
STORE=Sources/Shared/SharedStore.swift
for f in "$SUBJECT" "$VIEWS" "$METRICS" "$TALLY" "$STORE" \
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
fail_when_absent "small is one headline + detail" "$VIEWS" 'ClusterHeadline(entry: entry, size: 32)'
fail_when_absent "circular uses the up/total gauge" "$VIEWS" 'Gauge(value:'

fail_when_absent "the projection uses the shared FleetProjection" "$METRICS" 'typealias ClusterFleet = FleetProjection'
fail_when_absent "status jobs come from workloads.count" "$METRICS" 'response.workloads.count'

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
  Sources/Shared/ClusterMonitorViewModel.swift \
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
