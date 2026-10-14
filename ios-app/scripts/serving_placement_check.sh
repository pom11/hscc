#!/bin/bash
# serving_placement_check.sh — prove the unit->node attribution used by
# "Nodes & serving" (spec §5) puts every serving unit under the RIGHT machine.
#
# Why this gate exists: /v1/cluster/status carries no node field per unit
# (SharedModels.swift ClusterWorkload = name/tp/pp/container_id). The only place
# in the API that pairs a job with its hosts is live_status.output inside
# /v1/cluster/hosts — the raw `sparkrun status` TEXT the cluster engine forwards
# verbatim (hscc-cluster/hscc.py cmd_hosts -> run_cmd([SPARKRUN, "status"])). So
# the grouping is a text parse, and a text parse that is subtly wrong does not
# crash — it prints a Stop button beside the wrong node. That is the failure
# mode this harness is built to catch.
#
# Like model_decode_check.sh, it compiles the REAL sources —
#   Sources/HSCC/ServingPlacement.swift   (the parse, never redeclared)
#   Sources/HSCC/Models.swift
#   Sources/Shared/SharedModels.swift
#   Sources/HSCC/APIError.swift
# — into a plain macOS CLI, then asserts placement against the committed live
# capture (scripts/model_decode_check/fixtures/) AND the malformed blobs that
# would each name a wrong node if matching were loose.
#
# NEGATIVE TEST (guarded): break the parse deliberately and confirm this harness
# FAILS. A harness that cannot fail is worthless. Same shape as
# connection_banner_check / fleet_offline_check:
#   export SERVING_PLACEMENT_NEGATIVE_TEST=1
#
# Usage: scripts/serving_placement_check.sh
set -uo pipefail
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
cd "$(dirname "${BASH_SOURCE[0]}")/.."

export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
SDK=$(xcrun --sdk macosx --show-sdk-path 2>/dev/null) || {
  echo "error: no macOS SDK (is Xcode installed?)" >&2; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

real_parse="Sources/HSCC/ServingPlacement.swift"
real_models=(
  Sources/HSCC/Models.swift
  Sources/Shared/SharedModels.swift
  Sources/HSCC/APIError.swift
)
harness="scripts/serving_placement_check/main.swift"
# The Theme colours SharedModels.swift references render nothing on macOS but
# the types must exist — the same one-file shim model_decode_check uses.
theme_stub="scripts/model_decode_check/ThemeStub.swift"

for f in "$real_parse" "$harness" "$theme_stub"; do
  if [ ! -f "$f" ]; then echo "error: $f missing — did it get moved?" >&2; exit 1; fi
done
# The harness must drive the REAL parse; if these markers vanish it is testing a
# copy, which is the false-green these harnesses exist to prevent.
if ! grep -q "ServingPlacement.place" "$harness" ||
   ! grep -q "ServingPlacement.nodesByContainer" "$harness"; then
  echo "error: harness no longer calls the real parse (marker moved?)" >&2; exit 1
fi
if ! grep -q "Sources/HSCC/ServingPlacement.swift" "$SELF"; then
  echo "error: runner no longer compiles the real parse (marker moved?)" >&2; exit 1
fi

echo "compiling the REAL ServingPlacement + models + harness into a macOS CLI..."
if ! xcrun --sdk "$SDK" swiftc -o "$TMP/serving_placement_check" \
     "$real_parse" "${real_models[@]}" "$theme_stub" "$harness" 2>"$TMP/compile.err"; then
  echo "error: failed to compile the serving-placement harness — see below" >&2
  cat "$TMP/compile.err" >&2
  exit 1
fi
if [ -s "$TMP/compile.err" ]; then
  echo "warning: compiler output while building the harness:"; cat "$TMP/compile.err"
fi

FIXDIR=$(cd scripts/model_decode_check/fixtures && pwd)

if [ "${SERVING_PLACEMENT_NEGATIVE_TEST:-0}" = "1" ]; then
  # Prove the harness can fail: copy the real parse, break ONE rule (accept ANY
  # dotted token as a node address, so `999.1.1.1` and an image tag become
  # nodes), and require the harness to report failures.
  echo "NEGATIVE TEST: sabotaging the IPv4 rule and requiring the harness to fail..."
  mkdir -p "$TMP/sabotage"
  if ! sed -e 's/guard parts.count == 4 else { return false }/guard parts.count >= 2 else { return false }/' \
       -e 's/guard let value = Int(part), (0...255).contains(value) else { return false }/guard Int(part) != nil else { return false }/' \
       "$real_parse" > "$TMP/sabotage/ServingPlacement.swift"; then
    echo "error: negative-test sed failed — the sabotage anchor moved" >&2; exit 1
  fi
  if ! cmp -s "$real_parse" "$TMP/sabotage/ServingPlacement.swift"; then
    if xcrun --sdk "$SDK" swiftc -o "$TMP/sabotaged" \
         "$TMP/sabotage/ServingPlacement.swift" "${real_models[@]}" "$theme_stub" "$harness" \
         2>"$TMP/sab.err"; then
      if "$TMP/sabotaged" "$FIXDIR" >"$TMP/sab.out" 2>&1; then
        echo "NEGATIVE TEST FAILED: the harness passed with a sabotaged parse — it is not load-bearing" >&2
        exit 1
      fi
      echo "negative test OK: the sabotaged parse was caught (harness exited non-zero)"
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

"$TMP/serving_placement_check" "$FIXDIR"
rc=$?
if [ "$rc" = "0" ]; then
  echo "serving placement gate green (fixture-driven — run SERVING_PLACEMENT_NEGATIVE_TEST=1 to prove it can fail)"
fi
exit $rc
