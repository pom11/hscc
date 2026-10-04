#!/bin/bash
# turn_state_check.sh — prove the t_7cc2e7d5 turn-state mapping headlessly.
#
# The StreamingChatStore runs on an iOS runtime (MainActor + Combine) we can't
# load on this host, but the only NEW logic it folds over live frames is the
# pure TurnState.derive mapping (system kind -> working / awaitingApproval /
# idle). Following the established pattern (chat_state_check slices ChatEntry,
# streaming_check compiles the real core), this script:
#
#   1. slices the REAL TurnState enum (with derive) out of
#      StreamingChatStore.swift — the exact code the app runs, never a
#      redeclaration;
#   2. compiles it with the REAL SessionEvent decode layer + JSONValue +
#      SessionStreamCursor + a harness into a plain macOS CLI;
#   3. feeds real wire JSON (system handoff_pending / working / session_busy,
#      an assistant message) through real decode -> derive and asserts the
#      handoff-pending and working states the app must surface.
#
# Run whenever StreamingChatStore.swift's TurnState mapping changes.
# Usage: scripts/turn_state_check.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
SDK=$(xcrun --sdk macosx --show-sdk-path 2>/dev/null) || {
  echo "error: no macOS SDK (is Xcode installed?)" >&2; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

store="Sources/HSCC/Views/StreamingChatStore.swift"
# Slice the REAL TurnState enum (with its doc comment) out of the store so we
# test the exact mapping the app folds over, not a redeclaration. The enum must
# be present and terminated before the convenience-alias marker below it.
if ! grep -q '^enum TurnState: Equatable {' "$store"; then
  echo "error: could not locate the real TurnState enum (marker moved?)" >&2
  exit 1
fi
awk '/^enum TurnState: Equatable \{/{f=1} f{print} f && /^}/{exit}' "$store" > "$TMP/TurnState.swift"
if ! grep -q 'static func derive(from event: SessionEvent' "$TMP/TurnState.swift"; then
  echo "error: sliced TurnState lacks derive (t_7cc2e7d5 gone?)" >&2
  exit 1
fi
# The slice is a standalone file — prepend the import the Codable/Foundation
# types need to compile.
{ printf 'import Foundation\n\n'; cat "$TMP/TurnState.swift"; } > "$TMP/TurnState.swift.tmp"
mv "$TMP/TurnState.swift.tmp" "$TMP/TurnState.swift"

# Slice the REAL self-contained JSONValue enum out of Models.swift (compiling
# all of Models.swift drags in Speakable/HSCCError and the whole response model
# layer — not needed to prove the turn-state mapping).
if ! grep -q '^enum JSONValue: Decodable {' Sources/HSCC/Models.swift; then
  echo "error: could not locate the real JSONValue enum (marker moved?)" >&2
  exit 1
fi
awk '/^enum JSONValue: Decodable \{/{f=1} f{print} f && /^}/{exit}' Sources/HSCC/Models.swift > "$TMP/JSONValue.swift"
{ printf 'import Foundation\n\n'; cat "$TMP/JSONValue.swift"; } > "$TMP/JSONValue.swift.tmp"
mv "$TMP/JSONValue.swift.tmp" "$TMP/JSONValue.swift"

real_sources=(
  "$TMP/JSONValue.swift"             # real JSONValue
  Sources/HSCC/SessionEvent.swift    # real decode layer
  "$TMP/TurnState.swift"             # real TurnState.derive
)
harness=(
  scripts/turn_state_check/main.swift
  scripts/turn_state_check/SpeakableStub.swift
)

echo "compiling the REAL SessionEvent decode + TurnState.derive + harness..."
if ! xcrun --sdk "$SDK" swiftc -o "$TMP/turn_state_check" \
     "${real_sources[@]}" "${harness[@]}" 2>"$TMP/compile.err"; then
  echo "error: failed to compile — see below" >&2
  cat "$TMP/compile.err" >&2
  exit 1
fi

"$TMP/turn_state_check"
exit $?
