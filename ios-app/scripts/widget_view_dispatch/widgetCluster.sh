#!/bin/bash
# widget_view_dispatch/widgetCluster.sh — the CLUSTER widget surface
# (WidgetSurfaceKind.widgetCluster).
#
# Subject: Sources/HSCCWidgets/ClusterWidgetViews.swift. Not registry-driven —
# this surface predates the registry (t_5c554c5b), so it has no setting(for:)
# and no cadence check; it owns the invariant the rest of the directory inherits.
#
# The dispatch is the if/else-if chain in SmallClusterWidget.body and
# MediumClusterWidget.body. This file mirrors that chain and exhaustively checks
# every (state, configured) pair, and enforces the audit-3 invariant: ANY
# unconfigured entry MUST show the "Set up the app" setup invite. There is no iOS
# runtime on this host, so this proves the LOGIC of which view wins and the
# invariant, not the rendered pixels.
#
# Moved verbatim out of widget_view_dispatch_check.sh (lines 14-50 at c270ba84)
# by t_2c22e60d. Run standalone, or through the aggregator:
#   ./ios-app/scripts/widget_view_dispatch/widgetCluster.sh
set -uo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

# Prove the REAL source still takes the unconfigured gate before anything else.
# The gate moved into `ClusterEntry.text(for:)` (one owner for all six
# families), so the check reads that rule where it lives and requires every
# family to route through it.
copy=$(awk '/var shortGateLabel: String \{/ {f=1} f {print} f && /^    \}$/ {exit}' Sources/HSCCWidgets/ClusterWidgetMetrics.swift)
u=$(printf '%s\n' "$copy" | grep -n '!configured' | head -1 | cut -d: -f1)
p=$(printf '%s\n' "$copy" | grep -n 'Paused' | head -1 | cut -d: -f1)
if [ -n "$u" ] && [ -n "$p" ] && [ "$u" -lt "$p" ]; then
  echo "source shortGateLabel checks !configured (line $u) before Paused (line $p)"
else
  echo "❌ source order regressed: unconfigured no longer checked first"; exit 1
fi
families=$(grep -c 'entry.text(for:' Sources/HSCCWidgets/ClusterWidgetViews.swift)
echo "source family text routes = $families (want >= 6)"
[ "$families" -ge 6 ] || { echo "❌ a cluster family bypasses the shared gate/text model"; exit 1; }

cat > "$TMPDIR/widget_dispatch_$$.swift" <<'SWIFT'
import Foundation
enum S: String, CaseIterable { case serving, waking, down, unreachable, unknown }

// The FIXED order (matches the committed source): unconfigured wins over unreachable.
func dispatchView(state: S, configured: Bool) -> String {
    if !configured              { return "unconfiguredView     <-- 'Set up the app' invite" }
    else if state == .unreachable { return "unreachableStateView <-- stale, with age" }
    else if state == .unknown   { return "unknownView (falls through to normal layout)" }
    else                          { return "normal state layout (serving/waking/down)" }
}

var fail = false
for s in S.allCases {
    for c in [false, true] {
        let v = dispatchView(state: s, configured: c)
        print("state=" + s.rawValue + " configured=" + String(c) + " -> " + v)
        if !c && v.hasPrefix("unconfiguredView") == false {
            print("  WARN: unconfigured entry did NOT yield the setup invite")
            fail = true
        }
    }
}
if fail { print("RESULT: FAIL - some unconfigured entry shows a non-setup view"); exit(1) }
print("RESULT: OK - every unconfigured entry invites setup")
SWIFT

SDK=$(wvd_sdk) || exit 1
xcrun swiftc -sdk "$SDK" -o "$TMPDIR/widget_dispatch_$$" "$TMPDIR/widget_dispatch_$$.swift" 2>&1 || { echo "compile failed" >&2; exit 1; }
"$TMPDIR/widget_dispatch_$$"
rc=$?
rm -f "$TMPDIR/widget_dispatch_$$" "$TMPDIR/widget_dispatch_$$.swift"

exit $rc
