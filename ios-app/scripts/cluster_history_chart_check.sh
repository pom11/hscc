#!/bin/bash
# cluster_history_chart_check.sh — execute the shared cluster-history chart layer.
#
# Subject: Sources/Shared/ClusterHistorySeries.swift (t_b6512c2e) — the metric
# set a history surface may request, the series it builds off a
# GET /v1/cluster/history answer, and the pixel-width reduction that must not
# move a min or a max. Like connection_probe_check.sh / fleet_models_check.sh
# this compiles the REAL source plus the REAL wire types into a plain macOS CLI
# and runs it: no redeclaration, no network, no simulator.
#
# Why a gate of its own: the two claims it makes are behavioural, not shape.
# model_decode_check.sh proves the payload DECODES; it cannot notice that a
# chart reduction dropped the spike the operator opens the widget to see, nor
# that a 2 s point got averaged together with a 5-minute one. Both are lies a
# green compile and a green decode happily ship.
#
# The fixtures are the ROUTE's own bytes: scripts/make_cluster_history_fixture.py
# fills a store through the recorder's write path (MetricsRecorder.tick) and
# calls routes_cluster.handle_cluster_history, then writes what came back. That
# is why this gate also fails when the recorder's vocabulary drifts: the second
# leg below compares the Swift metric keys against
# hscc_daemon/metrics_history.py's HOST_COLUMNS / ENGINE_KEYS directly.
#
# NEGATIVE TEST (guarded): break the reduction deliberately and confirm the
# harness FAILS. A harness that cannot fail is worthless:
#   export CLUSTER_HISTORY_NEGATIVE_TEST=1
#
# Usage: scripts/cluster_history_chart_check.sh

set -uo pipefail
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
cd "$(dirname "${BASH_SOURCE[0]}")/.."

export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
SDK=$(xcrun --sdk macosx --show-sdk-path 2>/dev/null) || {
  echo "error: no macOS SDK (is Xcode installed?)" >&2; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

subject="Sources/Shared/ClusterHistorySeries.swift"
real_models=(
  Sources/Shared/SharedModels.swift
  Sources/Shared/SharedStore.swift
  Sources/Shared/WidgetSurfaceKind.generated.swift
)
harness="scripts/cluster_history_chart_check/main.swift"
theme_stub="scripts/model_decode_check/ThemeStub.swift"
fixture_dir="scripts/model_decode_check/fixtures"
vocab="scripts/cluster_history_metric_vocabulary.py"

for f in "$subject" "$harness" "$theme_stub" "$vocab"; do
  if [ ! -f "$f" ]; then echo "error: $f missing — did it get moved?" >&2; exit 1; fi
done
for f in v1_cluster_history_6h.json v1_cluster_history_6h_engine_only.json v1_cluster_history_15m.json v1_cluster_history_7d.json; do
  if [ ! -f "$fixture_dir/$f" ]; then
    echo "error: $fixture_dir/$f missing — regenerate with scripts/make_cluster_history_fixture.py" >&2
    exit 1
  fi
done

# The harness must drive the REAL layer; if these markers move it is testing a
# copy, which is the false-green these harnesses exist to prevent.
if ! grep -q "ClusterHistoryDownsampler.downsample" "$harness" ||
   ! grep -q "ClusterHistorySeriesSet.make" "$harness"; then
  echo "error: harness no longer drives the real history layer (markers moved?)" >&2
  exit 1
fi
if ! grep -q "Sources/Shared/ClusterHistorySeries.swift" "$SELF"; then
  echo "error: runner no longer compiles the real history layer (marker moved?)" >&2
  exit 1
fi

# The label rule (apps 1.4.4). A chart names a node exactly the way every other
# surface names it — `MonitorNode.nodeLabel`, the last octet — and never through
# `MonitorNode.shortTail`, which keeps its dot because it PAIRS rows (a serving
# row with its host row) rather than naming them. No harness in this repo
# compiles ClusterGraphReading.swift, so this is content-anchored rather than
# executed: weaker than a byte, and named as such on purpose. The bytes the
# legend produces are asserted where they can be — `scripts/model_decode_check/
# node_label.swift` pins the labeler itself, including the port-free octet and
# the dot the tail keeps.
for label_src in Sources/Shared/ClusterGraphReading.swift \
                 Sources/HSCCWidgets/GraphWidgetEntry.swift; do
  if grep -q "MonitorNode.shortTail" "$label_src"; then
    echo "error: $label_src names a node through the IDENTITY tail again — " >&2
    echo "       every node label goes through MonitorNode.nodeLabel (1.4.4)" >&2
    exit 1
  fi
  if ! grep -q "MonitorNode.nodeLabel" "$label_src"; then
    echo "error: $label_src no longer routes node names through the shared labeler" >&2
    exit 1
  fi
done
echo "label-rule leg: the chart's node labels go through the shared labeler PASS"

echo "compiling the REAL history layer + wire models + harness into a macOS CLI..."
if ! xcrun --sdk "$SDK" swiftc -o "$TMP/cluster_history_chart_check" \
     "$subject" "${real_models[@]}" "$theme_stub" "$harness" 2>"$TMP/compile.err"; then
  echo "error: failed to compile the history chart layer — see below" >&2
  cat "$TMP/compile.err" >&2
  exit 1
fi
# A warning here is a finding: build_check.sh holds every target to 0 warnings,
# and the same source compiles into all of them.
if [ -s "$TMP/compile.err" ]; then
  echo "error: the history layer compiled with diagnostics — treated as findings:" >&2
  cat "$TMP/compile.err" >&2
  exit 1
fi

echo "running the chart-layer checks against the route's own fixtures..."
"$TMP/cluster_history_chart_check" "$PWD/$fixture_dir"
harness_rc=$?

echo ""
echo "vocabulary leg: the Swift metric keys against the recorder's own columns..."
python3 "$vocab"
vocab_rc=$?

if [ "$harness_rc" -ne 0 ] || [ "$vocab_rc" -ne 0 ]; then
  echo ""
  echo "CLUSTER HISTORY CHART CHECK FAILED (harness rc=$harness_rc, vocabulary rc=$vocab_rc)"
  exit 1
fi
echo ""
echo "CLUSTER HISTORY CHART CHECK OK (harness + vocabulary leg both green)"
exit 0
