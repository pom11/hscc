#!/bin/bash
# live_activity_rehydration_check.sh — prove the re-hydration STALENESS DECIDERS.
#
# PART 1 — the fleet-wake activity. The launch sweep reads `Activity.activities`,
# which only exists on iOS at runtime — no ActivityKit here. So the DECIDER
# logic (transcribed from LiveActivityManager.sweepLeftoverWakes) is compiled
# into a macOS CLI and the full decision table is asserted. See main.swift.
#
# PART 2 — the running-cards activity (t_f9cc34e0). NOT transcribed. The two
# marker-bracketed blocks in Sources/Shared/CardsRunningActivity.swift
# (`GATE-SLICE-BEGIN/END: cards-reading-rules` and `…: cards-orphan-decision`)
# are sliced out of the source file at gate time and compiled alongside the
# harness, so what executes is the code the app's launch sweep calls
# (CardsRunningOrphanRule) and the code the extension's views draw
# (CardsRunningReading). Slicing is why a rule change cannot be shipped while
# the table still passes: a moved or renamed marker fails this script by name
# instead of quietly shrinking the table, which is the failure mode every
# path-anchored harness in this directory has eventually died of.
#
# PART 3 — the cluster-health activity (t_0120475a). The same arrangement in a
# SECOND file: the two blocks in Sources/Shared/ClusterHealthActivity.swift
# (`…: cluster-health-reading-rules` and `…: cluster-health-orphan-decision`)
# are sliced by the identical awk rule, so the table executes the
# `ClusterHealthOrphanRule` that `ClusterHealthActivitySweep` applies at launch
# and the words, tally and colour that `ClusterHealthLiveActivity` draws. The
# specs below therefore name the FILE each marker lives in — a spec that only
# named the marker would silently start slicing the wrong file the day a rule
# moved, and a green table would be proving nothing.
#
# Every sliced block is Foundation-only on purpose (no ActivityKit, no Theme),
# which is what lets it compile on the macOS SDK with this harness.
#
# SharedModels.swift + model_decode_check/ThemeStub.swift are compiled in for
# exactly one reason: PART 3 asserts this activity's state table against the
# real `ClusterState` the app and the widget share — the one agreement a sliced
# copy of a table cannot prove about itself. No other model is used here, and
# nothing in this script decodes a fixture (that is model_decode_check's job).
#
# Usage: scripts/live_activity_rehydration_check.sh
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

CARDS_SRC=Sources/Shared/CardsRunningActivity.swift
CLUSTER_SRC=Sources/Shared/ClusterHealthActivity.swift
OUT="$(mktemp -d)"; trap 'rm -rf "$OUT"' EXIT

slice() {  # slice <file> <marker-name> — print the bracketed block, markers excluded
  awk -v b="// GATE-SLICE-BEGIN: $2" -v e="// GATE-SLICE-END: $2" \
      '$0==b{s=1;next} $0==e{s=0} s' "$1"
}

printf 'import Foundation\n' > "$OUT/SlicedRules.swift"
# <file>|<marker name>|<the func that block must declare>
for spec in "$CARDS_SRC|cards-reading-rules|displayCount" \
            "$CARDS_SRC|cards-orphan-decision|isOrphan" \
            "$CLUSTER_SRC|cluster-health-reading-rules|headline" \
            "$CLUSTER_SRC|cluster-health-orphan-decision|isOrphan"; do
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

swiftc -o "$OUT/check" \
  "$OUT/main.swift" "$OUT/SlicedRules.swift" \
  Sources/Shared/SharedModels.swift \
  scripts/model_decode_check/ThemeStub.swift 2>&1 || {
  echo "compile failed" >&2; exit 1; }

"$OUT/check"
