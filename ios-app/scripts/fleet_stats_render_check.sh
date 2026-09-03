#!/bin/bash
# fleet_stats_render_check.sh — compile the REAL models + the by_day/activity
# render-replica harness into a macOS CLI, then prove the render mapping against
# a captured /v1/fleet/stats payload.
#
# Proves (against real wire data):
#   1. FleetStatsResponse decodes real /v1/fleet/stats (by_day + activity present).
#   2. shortDay renders ISO date -> "MM-DD", chronological sort.
#   3. parseToolPair maps top_tools [[name,count]] -> (String,Int) rows.
#   4. Tool-calls-by-profile mapping.
#   The bar widths printed are computed exactly as FleetView does (value/max*140).
#
# Why a replica and not FleetView.swift itself: shortDay/parseToolPair live in a
# SwiftUI struct; a headless CLI cannot exercise @ViewBuilder. build_check.sh
# already proves the real file compiles. This harness proves the DATA -> ROW
# mapping is correct against the real payload. Keep the two replica funcs in
# fleet_stats_render_check/main.swift byte-identical to FleetView.swift.
#
# Usage: scripts/fleet_stats_render_check.sh [capture_dir]
#   [capture_dir] defaults to the most recent scripts/live_captures/<ts>/ whose
#   v1_fleet_stats.json exists.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
SDK=$(xcrun --sdk macosx --show-sdk-path 2>/dev/null) || {
  echo "error: no macOS SDK (is Xcode installed?)" >&2; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

real_models=(
  Sources/HSCC/Models.swift
  Sources/Shared/SharedModels.swift
  Sources/HSCC/APIError.swift
)
harness=(
  scripts/model_decode_check/ThemeStub.swift
  scripts/fleet_stats_render_check/main.swift
)

if ! xcrun --sdk "$SDK" swiftc -o "$TMP/render_check" \
     "${real_models[@]}" "${harness[@]}" 2>"$TMP/compile.err"; then
  echo "error: failed to compile the real models + harness — see below" >&2
  cat "$TMP/compile.err" >&2
  exit 1
fi

# Locate a stats capture: explicit capture dir arg, else newest capture dir,
# else the standalone /tmp capture.
if [ -n "${1:-}" ]; then
  STATSFILE="$1/v1_fleet_stats.json"
else
  CAPDIR=$(ls -dt scripts/live_captures/*/ 2>/dev/null | head -1)
  STATSFILE="${CAPDIR:-.}/v1_fleet_stats.json"
fi
if [ ! -f "$STATSFILE" ]; then
  echo "error: no stats capture at $STATSFILE — run scripts/capture_live.sh first" >&2
  exit 1
fi
echo "stats capture: $STATSFILE"
"$TMP/render_check" "$STATSFILE"
exit $?
