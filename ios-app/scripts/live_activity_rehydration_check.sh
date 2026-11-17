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
# Both blocks are Foundation-only on purpose (no ActivityKit, no Theme), which
# is what lets them compile on the macOS SDK with this harness.
#
# Usage: scripts/live_activity_rehydration_check.sh
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

SRC=Sources/Shared/CardsRunningActivity.swift
OUT="$(mktemp -d)"; trap 'rm -rf "$OUT"' EXIT

slice() {  # slice <marker-name> — print the bracketed block, markers excluded
  awk -v b="// GATE-SLICE-BEGIN: $1" -v e="// GATE-SLICE-END: $1" \
      '$0==b{s=1;next} $0==e{s=0} s' "$SRC"
}

printf 'import Foundation\n' > "$OUT/CardsRules.swift"
for spec in "cards-reading-rules:displayCount" "cards-orphan-decision:isOrphan"; do
  name="${spec%%:*}"; want="${spec##*:}"
  grep -q "^// GATE-SLICE-BEGIN: ${name}$" "$SRC" || {
    echo "FAIL: $SRC has no 'GATE-SLICE-BEGIN: ${name}' marker." >&2
    echo "      The running-cards rules moved — re-point this gate at them." >&2
    echo "      Deleting these assertions instead would un-prove the surface." >&2
    exit 1
  }
  slice "$name" > "$OUT/slice.swift"
  [ -s "$OUT/slice.swift" ] || { echo "FAIL: empty slice for '$name' in $SRC" >&2; exit 1; }
  # The range must be the block that DECLARES the rule, not a comment window
  # that happens to sit between two markers.
  grep -q "func ${want}" "$OUT/slice.swift" || {
    echo "FAIL: slice '$name' does not declare '${want}' — wrong range sliced" >&2
    exit 1
  }
  cat "$OUT/slice.swift" >> "$OUT/CardsRules.swift"
done

cp scripts/live_activity_rehydration_check/main.swift "$OUT/main.swift"

swiftc -o "$OUT/check" "$OUT/main.swift" "$OUT/CardsRules.swift" 2>&1 || {
  echo "compile failed" >&2; exit 1; }

"$OUT/check"
