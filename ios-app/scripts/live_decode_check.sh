#!/bin/bash
# live_decode_check.sh — decode REAL live captures against the REAL models,
# and tell POPULATED from all-nil.
#
# model_decode_check.sh decodes the committed hand-written fixtures. Those
# prove the models match what we THINK the server returns, but they never prove
# the server behaves. This harness feeds the live captures (scripts/capture_live.sh)
# through the same compiled models and additionally flags all-nil decodes — the
# all-optional models that "decode" an error body into a silent empty screen.
#
# What it compiles (all real, none redeclared here):
#   Sources/HSCC/Models.swift
#   Sources/Shared/SharedModels.swift
#   Sources/HSCC/APIError.swift
#   Sources/HSCC/SessionEvent.swift
# plus the same Theme shim model_decode_check uses (a UI design token, not a
# model — needed only because SharedModels.swift references Theme.Semantic.*).
# The population check lives in main.swift and uses Mirror reflection, so it
# needs no per-model knowledge.
#
# Usage: scripts/live_decode_check.sh [capture_dir]
#   [capture_dir] defaults to the most recent scripts/live_captures/<ts>/.
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
  Sources/HSCC/SessionEvent.swift
)
harness=(
  scripts/model_decode_check/ThemeStub.swift
  scripts/live_decode_check/main.swift
)

echo "compiling real models + live-check harness into a macOS CLI..."
if ! xcrun --sdk "$SDK" swiftc -o "$TMP/live_check" \
     "${real_models[@]}" "${harness[@]}" 2>"$TMP/compile.err"; then
  echo "error: failed to compile the real model sources — see below" >&2
  cat "$TMP/compile.err" >&2
  exit 1
fi

# Pick the capture dir: explicit arg, or the newest live_captures/<ts>/.
if [ -n "${1:-}" ]; then
  CAPDIR="$1"
else
  CAPDIR=$(ls -dt scripts/live_captures/*/ 2>/dev/null | head -1)
  if [ -z "$CAPDIR" ]; then
    echo "error: no captures found — run scripts/capture_live.sh first" >&2
    exit 1
  fi
  CAPDIR=$(cd "$CAPDIR" && pwd)
fi
echo "capture dir: $CAPDIR"
"$TMP/live_check" "$CAPDIR"
exit $?
