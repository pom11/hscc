#!/bin/bash
# streaming_check.sh — prove the REAL streaming transcript aggregation headlessly.
#
# Compiles the ACTUAL StreamingTranscript.swift plus the real decode layer
# (Models/SharedModels/APIError/SessionEvent/SessionStreamCursor) into a plain
# macOS CLI and replays committed wire fixtures, asserting the composed chat
# rows (message token aggregation, tool start+finish collapsing, distinct
# single-frame rows, unknown-type degradation).
#
# This is the LIVE-chat-view half of the "no iOS runtime on this host" rule:
# the aggregation core is pure Foundation, so a macOS CLI is the faithful
# fixture runner — exactly like model_decode_check.sh proves the decode layer
# and reconnect_check.sh proves the cursor.
#
# Run this whenever StreamingTranscript.swift or SessionEvent.swift changes.
# Usage: scripts/streaming_check.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
SDK=$(xcrun --sdk macosx --show-sdk-path 2>/dev/null) || {
  echo "error: no macOS SDK (is Xcode installed?)" >&2; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

real_sources=(
  Sources/HSCC/Models.swift
  Sources/Shared/SharedModels.swift
  Sources/HSCC/APIError.swift
  Sources/HSCC/SessionEvent.swift
  Sources/HSCC/SessionStreamCursor.swift
  Sources/HSCC/StreamingTranscript.swift
)
harness=(
  scripts/model_decode_check/ThemeStub.swift   # SharedModels references Theme colors
  scripts/streaming_check/main.swift
)

echo "compiling real sources + harness into a macOS CLI..."
if ! xcrun --sdk "$SDK" swiftc -o "$TMP/streaming_check" \
     "${real_sources[@]}" "${harness[@]}" 2>"$TMP/compile.err"; then
  echo "error: failed to compile — see below" >&2
  cat "$TMP/compile.err" >&2
  exit 1
fi

"$TMP/streaming_check"
exit $?
