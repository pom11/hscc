#!/bin/bash
# fleet_models_check.sh — prove the row derivation behind Fleet's read-only
# Models section (Sources/HSCC/FleetServing.swift, ruling t_1897550c).
#
# Why this gate exists: the Models section turns the `/v1/cluster/status`
# payload into a headline, a mono caption, a state chip and ONE combined
# VoiceOver label. Three of those decisions fail silently in review:
#   1. The engine reports a workload by its RECIPE PATH, so the naive headline
#      prints `/home/<user>/.sparkrun-local/recipes/...` on a screen the card
#      is expected to screenshot (a home directory reaching a design artifact
#      is the address-leak class, not just ugly).
#   2. The feed repeats one name per container (three replicas in the committed
#      capture) and `ClusterWorkload.id` IS the name — `ForEach(state.workloads)`
#      therefore feeds SwiftUI duplicate ids, the defect where rows vanish or
#      double on refresh. `FleetServing.rows(from:)` must never emit duplicates.
#   3. The VoiceOver label must not drift from what the row shows (spec §9).
#
# Like serving_placement_check.sh / model_decode_check.sh, it compiles the REAL
# sources —
#   Sources/HSCC/FleetServing.swift   (the derivation, never redeclared)
#   Sources/HSCC/Models.swift
#   Sources/Shared/SharedModels.swift
#   Sources/HSCC/APIError.swift
# — into a plain macOS CLI, then asserts against the committed live capture
# (scripts/model_decode_check/fixtures/v1_cluster_status.json) plus synthetic
# edges (bare names, non-yaml extensions, unknown tp/pp, duplicate pairs).
#
# NEGATIVE TEST (guarded): break the derivation deliberately and confirm this
# harness FAILS. A harness that cannot fail is worthless. Same shape as
# serving_placement_check / fleet_offline_check:
#   export FLEET_MODELS_NEGATIVE_TEST=1
#
# Usage: scripts/fleet_models_check.sh
set -uo pipefail
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
cd "$(dirname "${BASH_SOURCE[0]}")/.."

export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
SDK=$(xcrun --sdk macosx --show-sdk-path 2>/dev/null) || {
  echo "error: no macOS SDK (is Xcode installed?)" >&2; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

real_derivation="Sources/HSCC/FleetServing.swift"
real_models=(
  Sources/HSCC/Models.swift
  Sources/Shared/SharedModels.swift
  Sources/HSCC/APIError.swift
)
harness="scripts/fleet_models_check/main.swift"
# The colours SharedModels.swift references render nothing on macOS but the
# types must exist — the same one-file shim model_decode_check uses.
theme_stub="scripts/model_decode_check/ThemeStub.swift"

for f in "$real_derivation" "$harness" "$theme_stub"; do
  if [ ! -f "$f" ]; then echo "error: $f missing — did it get moved?" >&2; exit 1; fi
done
# The harness must drive the REAL derivation; if these markers vanish it is
# testing a copy, which is the false-green these harnesses exist to prevent.
if ! grep -q "FleetServing.rows(from:" "$harness" ||
   ! grep -q "FleetServing.modelDisplayName" "$harness"; then
  echo "error: harness no longer calls the real derivation (marker moved?)" >&2; exit 1
fi
if ! grep -q "Sources/HSCC/FleetServing.swift" "$SELF"; then
  echo "error: runner no longer compiles the real derivation (marker moved?)" >&2; exit 1
fi

echo "compiling the REAL FleetServing + models + harness into a macOS CLI..."
if ! xcrun --sdk "$SDK" swiftc -o "$TMP/fleet_models_check" \
     "$real_derivation" "${real_models[@]}" "$theme_stub" "$harness" 2>"$TMP/compile.err"; then
  echo "error: failed to compile the fleet-models harness — see below" >&2
  cat "$TMP/compile.err" >&2
  exit 1
fi
if [ -s "$TMP/compile.err" ]; then
  echo "warning: compiler output while building the harness:"; cat "$TMP/compile.err"
fi

FIXDIR=$(cd scripts/model_decode_check/fixtures && pwd)

if [ "${FLEET_MODELS_NEGATIVE_TEST:-0}" = "1" ]; then
  # Prove the harness can fail: copy the real derivation, sabotage ONE rule
  # (headline = the raw workload name, i.e. the recipe path with a home
  # directory), and require the harness to report failures.
  echo "NEGATIVE TEST: sabotaging the headline trim and requiring the harness to fail..."
  mkdir -p "$TMP/sabotage"
  if ! sed -e 's/^        let leaf = name.components(separatedBy: "\/").last.map { \$0.isEmpty ? name : \$0 } ?? name$/        let leaf = name/' \
       "$real_derivation" > "$TMP/sabotage/FleetServing.swift"; then
    echo "error: negative-test sed failed — the sabotage anchor moved" >&2; exit 1
  fi
  if ! cmp -s "$real_derivation" "$TMP/sabotage/FleetServing.swift"; then
    if xcrun --sdk "$SDK" swiftc -o "$TMP/sabotaged" \
         "$TMP/sabotage/FleetServing.swift" "${real_models[@]}" "$theme_stub" "$harness" \
         2>"$TMP/sab.err"; then
      if "$TMP/sabotaged" "$FIXDIR" >"$TMP/sab.out" 2>&1; then
        echo "NEGATIVE TEST FAILED: the harness passed with a sabotaged headline trim — it is not load-bearing" >&2
        exit 1
      fi
      echo "negative test OK: the sabotaged trim was caught (harness exited non-zero)"
      grep -m3 "^FAIL" "$TMP/sab.out" | sed 's/^/    /'
      exit 0
    fi
    echo "error: sabotaged variant did not compile — anchor drift, fix this script" >&2
    cat "$TMP/sab.err" >&2
    exit 1
  fi
  echo "error: negative test sed changed nothing (anchor drift)" >&2
  exit 1
fi

"$TMP/fleet_models_check" "$FIXDIR"
rc=$?
if [ "$rc" = "0" ]; then
  echo "fleet Models-section gate green (fixture-driven — run FLEET_MODELS_NEGATIVE_TEST=1 to prove it can fail)"
fi
exit $rc
