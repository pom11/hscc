#!/bin/bash
# session_activity_check.sh — prove the session Live Activity derivation headlessly.
#
# Compiles the ACTUAL SessionActivitySummary.swift plus the real decode +
# aggregation layer (Models/SharedModels/APIError/SessionEvent/SessionStreamCursor/
# StreamingTranscript) into a plain macOS CLI and replays committed wire fixtures,
# asserting the derived Live Activity phase/headline/detail per row type.
#
# This is the session-Live-Activity half of the "no iOS runtime on this host"
# rule: the derivation is pure Foundation, and the rows it reads are the PRODUCT
# of decoding real session_event wire shapes — it proves we never invent progress,
# we render exactly what the streaming pipeline actually folded.
#
# Run whenever SessionActivitySummary.swift, StreamingTranscript.swift or
# SessionEvent.swift changes.
# Usage: scripts/session_activity_check.sh
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
  Sources/HSCC/SessionActivitySummary.swift
)
harness=(
  scripts/model_decode_check/ThemeStub.swift
  scripts/session_activity_check/StreamPhaseStub.swift
  scripts/session_activity_check/main.swift
)

echo "compiling real sources + harness into a macOS CLI..."
if ! xcrun --sdk "$SDK" swiftc -o "$TMP/session_activity_check" \
     "${real_sources[@]}" "${harness[@]}" 2>"$TMP/compile.err"; then
  echo "error: failed to compile — see below" >&2
  cat "$TMP/compile.err" >&2
  exit 1
fi

"$TMP/session_activity_check"
exit $?
