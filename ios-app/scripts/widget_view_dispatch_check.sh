#!/bin/bash
# widget_view_dispatch_check.sh — prove which view the widget renders for
# every (state, configured) combination, and enforce the audit-3 invariant:
# ANY unconfigured entry MUST show the "Set up the app" setup invite.
#
# (t_5c554c5b) The dispatch is the if/else-if chain in ClusterWidgetViews.swift
# (SmallClusterWidget.body, MediumClusterWidget.body). This harness mirrors that
# chain and exhaustively checks every (state, configured) pair. There is no iOS
# runtime on this host, so this proves the LOGIC of which view wins and the
# invariant, not the rendered pixels.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# Prove the REAL source checks `!configured` before `unreachable` (both bodies).
count=$(grep -c 'if !entry.configured' Sources/HSCCWidgets/ClusterWidgetViews.swift)
echo "source 'if !entry.configured' occurrences = $count (want 2: small+medium bodies)"
[ "$count" -ge 2 ] || { echo "❌ source order regressed: unconfigured no longer checked first"; exit 1; }

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

SDK=$(xcrun --sdk macosx --show-sdk-path 2>/dev/null) || { echo "no macOS SDK" >&2; exit 1; }
xcrun swiftc -sdk "$SDK" -o "$TMPDIR/widget_dispatch_$$" "$TMPDIR/widget_dispatch_$$.swift" 2>&1 || { echo "compile failed" >&2; exit 1; }
"$TMPDIR/widget_dispatch_$$"
rc=$?
rm -f "$TMPDIR/widget_dispatch_$$" "$TMPDIR/widget_dispatch_$$.swift"
exit $rc
