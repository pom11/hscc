#!/bin/bash
# snapshot_store_check.sh — prove the widget's last-known snapshot nodes
# round-trip through the REAL SnapshotStore encode/decode logic committed in
# Sources/Shared/SharedModels.swift.
#
# Why this exists (t_15a88458): the widget's SnapshotStore saved a last-known
# topology to the App Group so an unreachable window could show yesterday's
# real per-node state. encode used `|` for BOTH the node separator and the
# label/state separator, so decode (split on `|`, then re-split a token that no
# longer contained `|`) always recovered 0 nodes. The interleaving made the
# `.244|up|.246|up|…` format unrecoverable.
#
# This harness EXTRACTS the two private functions verbatim from the committed
# source (so it can never drift), re-homes them under a `SnapshotStore` enum
# with the same nested types, and asserts a multi-state round-trip. It cannot
# run on a device (no iOS runtime on this host); it proves the serialization
# contract executes correctly — the exact part that was broken.
#
# Usage: scripts/snapshot_store_check.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
SRC="Sources/Shared/SharedModels.swift"

# Extract the two functions verbatim from the source (no hand-copy -> no drift),
# then drop the `private` so the harness main can call them (bodies unchanged).
fns=$(awk '/private static func encodeNodes/,/^    \}/' "$SRC")
fns="$fns"$'\n'"$(awk '/private static func decodeNodes/,/^    \}/' "$SRC")"
fns=${fns//private static func/static func}

cat > "$TMPDIR/snap_check_$$.swift" <<SWIFT
import Foundation
// Mirror the real nested types exactly so the extracted code type-checks.
enum ClusterState: String { case serving, waking, down, unreachable, unknown }
struct TopologyNode {
    let label: String; let state: NodeState
    enum NodeState: String { case up, busy, warn, down, unknown }
}
struct TopologyPair { let nodes: [TopologyNode]; let role: String }
enum SnapshotStore {
$fns
}
let pairs = [
    TopologyPair(nodes: [TopologyNode(label: ".244", state: .up),   TopologyNode(label: ".246", state: .busy)], role: "orchestrator"),
    TopologyPair(nodes: [TopologyNode(label: ".247", state: .warn), TopologyNode(label: ".248", state: .down)], role: "worker"),
]
let encoded = SnapshotStore.encodeNodes(pairs)
print("encoded = '\\(encoded)'")
let decoded = SnapshotStore.decodeNodes(encoded)
print("decoded count = \\(decoded.count) (want 4)")
var ok = decoded.count == 4
if decoded.count == 4 {
    for (i, n) in decoded.enumerated() {
        let e = pairs.flatMap { node in node.nodes }[i]
        if n.label != e.label || n.state != e.state { ok = false; print("  MISMATCH \\(i)") }
        else { print("  ok \\(n.label) \\(n.state.rawValue)") }
    }
}
guard SnapshotStore.decodeNodes(nil).isEmpty else { print("empty should be []"); exit(1) }
if ok { print("✅ REAL SnapshotStore encode/decode round-trip OK") }
else { print("❌ SNAPSHOT ROUND-TRIP FAILED"); exit(1) }
SWIFT

SDK=$(xcrun --sdk macosx --show-sdk-path 2>/dev/null) || { echo "no macOS SDK" >&2; exit 1; }
xcrun swiftc -sdk "$SDK" -o "$TMPDIR/snap_check_$$" "$TMPDIR/snap_check_$$.swift" 2>&1 || { echo "compile failed" >&2; exit 1; }
"$TMPDIR/snap_check_$$"
rc=$?
rm -f "$TMPDIR/snap_check_$$" "$TMPDIR/snap_check_$$.swift"
exit $rc
