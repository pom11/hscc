#!/bin/bash
# cluster_widget_check.sh — prove what the cluster widget shows, by EXECUTING the
# committed source.
#
# Why this exists: `Sources/HSCCWidgets/ClusterWidgetViews.swift` + its provider
# `ClusterWidget.swift` render a fleet state an operator acts on. Four things
# about them compile perfectly and lie anyway, so they are asserted, not read:
#
#   * THE MODEL COUNT. "Models served" is a count of DISTINCT `cluster_id`
#     values across hosts — summing per-host workload lists double-counts every
#     tensor-parallel model (the committed fixture is 4 hosts / 2 models).
#   * MAX, NOT MEAN. GPU util and both temperatures take the worst node; a mean
#     of four hosts reads 48% for a fleet with two GPUs pinned at 96%.
#   * NIL IS NOT ZERO. §4.4 measured the fleet-down answer as 38 B with no
#     `json` key, and a host can be listed with an `error`. A projection that
#     collapsed those to 0 would tell the operator the GPUs are idle.
#   * §7 REDACTION. `hosts[].workloads` carries `recipe` (an absolute path) and
#     container names in the same object; `MonitorWorkload` declares only
#     `cluster_id`, which is what makes them undecodable rather than undesired.
#
# It also enforces the two decisions this card made about the surface:
#   * the cluster half is TWO reads (`autodown/status` + `cluster/monitor`) and
#     no route string appears in a widget file — every read goes through
#     `WidgetDataClient`'s named methods, so the closed allowlist stays the gate;
#   * the board row (dataset 3) is fetched only for a family that draws it, so an
#     accessory refresh is those two requests (§2's budget).
#
# What it compiles (all real, nothing redeclared):
#   Sources/HSCCWidgets/ClusterWidgetMetrics.swift (subject — projection, entry,
#                                                  heat, text, board-row decision)
#   Sources/Shared/SharedModels.swift              (the wire types)
#   Sources/Shared/SharedStore.swift               (WidgetSurfaceKind's sections)
#   Sources/Shared/WidgetDataClient.swift          (the failure labels)
#   scripts/model_decode_check/ThemeStub.swift     (UI token shim SharedModels needs)
# plus scripts/cluster_widget_check/main.swift. The two WidgetKit files it came
# from are NOT compiled here — `accessory*` families are `@available(macOS,
# unavailable)`, which is why that layer was split out at all — and
# HSCCWidgetsBundle.swift's @main would collide with main.swift. NO NETWORK, no
# simulator, no device.
#
# Layout cannot run headless, so the harness executes the renderer-free halves
# and the greps below pin the view file's gating and dispatch order, so the
# mirror cannot drift silently.
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
STORE=Sources/Shared/SharedStore.swift
for f in "$SUBJECT" "$VIEWS" "$METRICS" "$STORE" \
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

# Every read must be a named method on WidgetDataClient: a bare route string in
# a widget file is a read that bypasses the allowlist test in
# widget_data_client_check.sh, and it does not fail to compile.
report "no /v1/ route literal in the widget file set (reads go through the client)" \
  "$(grep -n '"/v1/' "$SUBJECT" "$VIEWS" "$METRICS")"

# The read this card removed, and why: /v1/cluster/status answers 39 B with no
# workloads/total_hosts when the fleet is down (WIDGET_DATA_CONTRACT §4.4), which
# failed that response type's required keys and made the widget say "Can't reach
# the cluster" for a fleet the API had just reported DOWN.
report "cluster/status is not read by this surface again" \
  "$(grep -n 'clusterStatus\|ClusterStatusResponse' "$SUBJECT" "$VIEWS")"

# §2's budget for the cluster half, pinned as a count: adding a third
# unconditioned read to this surface must fail here and be argued in the open.
reads=$(grep -c 'WidgetData.read(' "$SUBJECT")
report "exactly 2 WidgetData.read calls (autodown/status + cluster/monitor), got $reads" \
  "$([ "$reads" -eq 2 ] || echo x)"
fail_when_absent "the state read is autodown/status" "$SUBJECT" '$0.autodownStatus()'
fail_when_absent "the metric read is cluster/monitor" "$SUBJECT" '$0.clusterMonitor()'

# The board row must be refused BEFORE any kanban request, and the refusal must
# be the family/section decision this card made.
guard_line=$(grep -n 'guard enabled else' "$SUBJECT" | head -1 | cut -d: -f1)
kanban_line=$(grep -n 'kanbanRunning()' "$SUBJECT" | head -1 | cut -d: -f1)
report "the board row's guard precedes its first kanban request (guard=${guard_line:-none} kanban=${kanban_line:-none})" \
  "$({ [ -n "$guard_line" ] && [ -n "$kanban_line" ] && [ "$guard_line" -lt "$kanban_line" ]; } || echo x)"
report "no kanban read in the view file (all three live in boardWork)" \
  "$(grep -n 'client\.kanban\|kanbanRunning()\|kanbanBlocked()\|kanbanStale(' "$VIEWS")"

# The board row is only *drawn* by the system families. WidgetKit makes the
# accessory cases unusable on the host, so the executed half can only assert the
# positive list (`showsBoardWork` true for systemSmall/Medium/Large/ExtraLarge);
# this pins the other half — an accessory body must not read the board fields at
# all, so a family that never pays for the row can never draw it either.
accessory_range=$(awk '/^struct CircularClusterWidget/,0' "$VIEWS")
report "the three accessory views never read the board row's fields" \
  "$(printf '%s\n' "$accessory_range" | grep -n 'runningCards\|queueDepth\|blockedCards\|boardWork')"
report "the system views do read them (the proof above would pass on a stripped widget)" \
  "$(grep -n 'runningCards' "$VIEWS" | sed -n '1p' | grep -v '^$' >/dev/null || echo x)"

# Families registered, and dispatched.
fail_when_absent "supportedFamilies offers accessoryCircular" "$VIEWS" '.accessoryCircular'
fail_when_absent "supportedFamilies offers accessoryRectangular" "$VIEWS" '.accessoryRectangular'
fail_when_absent "supportedFamilies offers accessoryInline" "$VIEWS" '.accessoryInline'
for fam in accessoryInline accessoryCircular accessoryRectangular; do
  fail_when_absent "the view dispatches .$fam" "$VIEWS" "case .$fam:"
done

# The setup-invite-first invariant (audit-3 / t_5c554c5b), now across the
# families that were added. `accessoryInline` is exempt by construction: it
# renders only `ClusterMetricText.inlineLine(..., gateLabel: entry.shortGateLabel)`,
# and shortGateLabel checks `!configured` before anything else.
count=$(grep -c 'if !entry.configured' "$VIEWS")
report "at least 4 bodies check !configured before any data (got $count)" \
  "$([ "$count" -ge 4 ] || echo x)"
fail_when_absent "shortGateLabel invites setup first" "$METRICS" 'if !configured { return "Set up in HSCC" }'

# The new section vocabulary: "nodes" is the canonical SharedStore identifier
# for per-node monitor metrics, and the view must actually gate on it.
fail_when_absent "widgetCluster's sections include nodes" "$STORE" \
  'case .widgetCluster:    return \["state", "topology", "models", "board", "nodes"\]'
fail_when_absent "the metric strip is gated on the nodes section" "$VIEWS" \
  'showSections.contains("nodes")'
fail_when_absent "the board row is gated on the board section" "$VIEWS" \
  'showSections.contains("board")'

# The snapshot is written only from a fully live read, so a half-degraded refresh
# cannot overwrite good last-known data.
fail_when_absent "SnapshotStore.save sits behind a live autodown read" "$SUBJECT" \
  'if case .live = autoOutcome, let fleet'

# §7 executed elsewhere; here, the shape that makes it structural.
wl_lets=$(sed -n '/^struct MonitorWorkload/,/^}/p' Sources/Shared/SharedModels.swift | grep -c "let ")
report "MonitorWorkload declares exactly 1 stored property (cluster_id), got $wl_lets" \
  "$([ "$wl_lets" -eq 1 ] || echo x)"
report "no path or container key is declared on the workload projection" \
  "$(sed -n '/^struct MonitorWorkload/,/^}/p' Sources/Shared/SharedModels.swift | grep -n 'recipe\|containers\|image\|name')"

# The decode trap this card hit: a stored property with a declaration default is
# NOT decoded (compiler warning only). These four keys must be declared bare.
for key in workloads cpu_temp_c gpu_temp_c cpu_load_1m; do
  report "$key is declared without a default (a default would silently skip decoding)" \
    "$(grep -n "let $key:.*= nil" Sources/Shared/SharedModels.swift)"
done

echo ""
echo "executed proofs (macOS compile of the real widget + shared sources)"

xcrun swiftc -sdk "$SDK" -o "$TMP/cluster_widget_check" \
  "$METRICS" \
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
  echo "CLUSTER WIDGET CHECK PASSED — read budget, node/model projection, nil-is-not-zero, §7 workload redaction and the accessory families' text rules all hold"
  exit 0
fi
echo "CLUSTER WIDGET CHECK FAILED"
exit 1
