#!/bin/bash
# live_activity_rehydration_check.sh — prove the ONE Live Activity's rules.
#
# The 2026-11-20 picks left a single activity, "Cluster watch" (A2), reusing the
# Cluster Health activity's plumbing. This gate therefore covers only
# `Sources/Shared/ClusterHealthActivity.swift`:
#
#   * `cluster-health-reading-rules` — every word, count and colour the Lock
#     Screen and all three Island regions draw, including the new A2 layouts
#     (header, four node columns, compact/minimal strings and footer);
#   * `cluster-health-orphan-decision` — the launch sweep's pure decision;
#   * `cluster-health-projection-rules` — how the sanctioned reads become the
#     pushed signals, including the stale-snapshot data clock (a snapshot keeps
#     its real `updatedAt`, never `Date()`).
#
# The blocks are SLICED out of the source at gate time and compiled with this
# harness, so a rule change cannot be shipped while the table still passes. The
# wake/monitor/running-cards/board-progress activities and their gates were
# removed with the activities themselves.
#
# Every sliced block is Foundation-only on purpose (no ActivityKit, no Theme),
# which is what lets it compile on the macOS SDK with this harness.
#
# Usage: scripts/live_activity_rehydration_check.sh
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

. scripts/swift_source_groups.sh

CLUSTER_SRC=Sources/Shared/ClusterHealthActivity.swift
OUT="$(mktemp -d)"; trap 'rm -rf "$OUT"' EXIT

slice() {  # slice <file> <marker-name> — print the bracketed block, markers excluded
  awk -v b="// GATE-SLICE-BEGIN: $2" -v e="// GATE-SLICE-END: $2" \
      '$0==b{s=1;next} $0==e{s=0} s' "$1"
}

printf 'import Foundation\n' > "$OUT/SlicedRules.swift"
# <file>|<marker name>|<the func that block must declare>
for spec in "$CLUSTER_SRC|cluster-health-reading-rules|headline" \
            "$CLUSTER_SRC|cluster-health-orphan-decision|isOrphan" \
            "$CLUSTER_SRC|cluster-health-projection-rules|signals"; do
  src="${spec%%|*}"; rest="${spec#*|}"; name="${rest%%|*}"; want="${rest##*|}"
  grep -q "^// GATE-SLICE-BEGIN: ${name}$" "$src" || {
    echo "FAIL: $src has no 'GATE-SLICE-BEGIN: ${name}' marker." >&2
    echo "      The ${name} rules moved — re-point this gate at them." >&2
    echo "      Deleting these assertions instead would un-prove the surface." >&2
    exit 1
  }
  slice "$src" "$name" > "$OUT/slice.swift"
  [ -s "$OUT/slice.swift" ] || { echo "FAIL: empty slice for '$name' in $src" >&2; exit 1; }
  # The range must be the block that DECLARES the rule, not a comment window
  # that happens to sit between two markers.
  grep -q "func ${want}" "$OUT/slice.swift" || {
    echo "FAIL: slice '$name' does not declare '${want}' — wrong range sliced" >&2
    exit 1
  }
  cat "$OUT/slice.swift" >> "$OUT/SlicedRules.swift"
done

cp scripts/live_activity_rehydration_check/main.swift "$OUT/main.swift"

# `ClusterNodeTally` and `ClusterMonitorViewModel` are compiled in, not sliced:
# they are the Foundation-only files the cluster widget also runs, and the
# projection calls them. Dropping either fails the compile rather than quietly
# narrowing what is proved.
swiftc -o "$OUT/check" \
  "$OUT/main.swift" "$OUT/SlicedRules.swift" \
  "${SWIFT_GROUP_WIDGET_CLIENT[@]}" \
  Sources/Shared/ClusterNodeTally.swift \
  Sources/Shared/ClusterMonitorViewModel.swift \
  scripts/model_decode_check/ThemeStub.swift 2>&1 || {
  echo "compile failed" >&2; exit 1; }

# The driver is ActivityKit + BackgroundTasks and cannot run here; these
# structural checks pin the two audit fixes so a future edit cannot quietly
# restore them (stale data dated "now", and Off becoming a one-minute poll).
DRIVER=Sources/HSCC/ClusterHealthActivityDriver.swift
gate_fail=0
grep -q 'ClusterHealthReading.snapshotDate(ageMinutes:' "$DRIVER" \
  && echo "  ok: a stale snapshot keeps its real date" \
  || { echo "  FAIL: stale snapshots are not dated from their age" >&2; gate_fail=1; }
grep -q 'showingStale ? dataAt' "$DRIVER" \
  && echo "  ok: a failed refresh dates staleDate from the snapshot" \
  || { echo "  FAIL: staleDate is refreshed to now" >&2; gate_fail=1; }
grep -q 'guard let cadence = refreshMinutes() else' "$DRIVER" \
  && echo "  ok: Off stops the poll instead of scheduling one minute" \
  || { echo "  FAIL: the Off guard is gone" >&2; gate_fail=1; }
grep -q 'max(1, setting().refreshMinutes)' "$DRIVER" \
  && { echo "  FAIL: the max(1, …) Off bug is back" >&2; gate_fail=1; } \
  || echo "  ok: no max(1, refreshMinutes) coercion"
[ "$gate_fail" -eq 0 ] || exit 1

"$OUT/check"
