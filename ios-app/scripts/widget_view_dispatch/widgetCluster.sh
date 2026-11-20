#!/bin/bash
# widget_view_dispatch/widgetCluster.sh — the CLUSTER widget surface
# (WidgetSurfaceKind.widgetCluster), C1 Native redesign (2026-11-20).
#
# Subject: Sources/HSCCWidgets/ClusterWidgetViews.swift. Every Home Screen
# family takes the unconfigured/paused gate in ONE step before it can draw a
# number, and the C1 layout is composed from the shared NativeStyleKit rather
# than re-typing fonts, colours or the stale rule.
#
# Run standalone, or through the aggregator:
#   ./ios-app/scripts/widget_view_dispatch/widgetCluster.sh
set -uo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

# Invite before pause: the accessory copy still owns this ordering.
copy=$(awk '/var shortGateLabel: String \{/ {f=1} f {print} f && /^    \}$/ {exit}' Sources/HSCCWidgets/ClusterWidgetMetrics.swift)
u=$(printf '%s\n' "$copy" | grep -n '!configured' | head -1 | cut -d: -f1)
p=$(printf '%s\n' "$copy" | grep -n 'Paused' | head -1 | cut -d: -f1)
if [ -n "$u" ] && [ -n "$p" ] && [ "$u" -lt "$p" ]; then
  echo "  ok: shortGateLabel checks !configured (line $u) before Paused (line $p)"
else
  echo "  FAIL: source order regressed: unconfigured no longer checked first"; exit 1
fi

V=Sources/HSCCWidgets/ClusterWidgetViews.swift
M=Sources/HSCCWidgets/ClusterWidgetMetrics.swift
K=Sources/HSCCWidgets/NativeStyleKit.swift
FAILURES=0

# (1) Every Home Screen family gates in one step, so no branch can draw a
# number on a paused or unconfigured surface.
n=$(grep -c 'if !entry.configured || entry.paused' "$V")
if [ "$n" -ge 3 ]; then
  wok "$n Home Screen bodies gate on !configured||paused (want >= 3)"
else
  wbad "only $n Home Screen bodies gate on !configured||paused (want >= 3)"
fi

# (2) The C1 layout is the new contract, not a copy of the old table.
for needle in 'let c1 = entry.c1' 'MeterBar(value:' 'AsOfStamp(date:' 'ClusterThroughputChart(points:' 'entry.throughput'; do
  if grep -qF -- "$needle" "$V"; then
    wok "C1 layout uses '$needle'"
  else
    wbad "C1 layout is missing '$needle'"
  fi
done
if grep -q 'struct ClusterC1' "$M" && grep -q 'var c1: ClusterC1' "$M"; then
  wok "the entry projects one ClusterC1 model for every C1 family"
else
  wbad "the entry does not own the ClusterC1 projection"
fi
if grep -q 'C1' "$K" || grep -q 'NativeStyle' "$V"; then
  wok "the shared NativeStyleKit supplies the C1 tokens"
else
  wbad "the C1 views do not use the shared NativeStyleKit"
fi

# (3) The chart is the 2 h total-throughput series, summed from the engine
# `gen_tps` rows — not a made-up host average.
if grep -q 'ClusterHistoryMetric.genTps' Sources/HSCCWidgets/ClusterWidget.swift; then
  wok "the large tile asks history for gen_tps"
else
  wbad "the large tile does not ask history for gen_tps"
fi
if grep -q 'sums\[sample.t, default: 0\] += value' "$M"; then
  wok "the chart series sums engine rows by timestamp"
else
  wbad "the chart series does not sum engine rows by timestamp"
fi

# (4) The old table path is gone from the C1 bodies.
if grep -q 'MonitorTablesBlock\|CLIMonitorHostTableView' "$V"; then
  wbad "a C1 body still draws the removed table view"
else
  wok "the C1 bodies draw no removed table view"
fi

# (5) The three accessory bodies are intentionally untouched by this card but
# must keep routing through the gated text model.
n=$(grep -cE 'entry\.text\(for: \.(inline|circular|rectangular)\)' "$V")
if [ "$n" -ge 2 ]; then
  wok "accessory families still route through the entry's gated text ($n sites)"
else
  wbad "only $n accessory sites route through the gate (want >= 2)"
fi

wvd_finish
