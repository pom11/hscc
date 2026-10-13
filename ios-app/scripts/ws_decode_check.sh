#!/usr/bin/env bash
# ws_decode_check — decode REAL captured API frames through the shipped
# parsers (ChatStreamFrame + isHelloEnvelope) and assert the decode shape.
#
# WHY this exists (iOS-chat-e2e 2026-10-13): the app's worst class of bugs is
# decode mismatches — the live serve<->app boundary drifted silently before
# (the stuck "Connecting to the live stream..." banner: the hello envelope
# carries "type", the client probed "kind"). Unit tests with synthetic
# fixtures did not catch it because the fixtures encoded the same wrong
# assumption. This check feeds frames captured VERBATIM from the live API
# (tests/fixtures/ws_frames/*.jsonl) through the exact production decoders.
#
# Usage:  ios-app/scripts/ws_decode_check.sh
# Exit:   0 all green, 1 decode/assertion failure, 2 toolchain/usage failure.
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

FIXTURE_DIR="tests/fixtures/ws_frames"
TMP="$(mktemp -d /tmp/ws_decode_check.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

FRAMEWORK="$(xcrun --sdk macosx --show-sdk-platform-path)/Developer/Library/Frameworks"
SWIFT_FILES=(Sources/HSCC/Models.swift Sources/Shared/SharedModels.swift
             Sources/HSCC/APIError.swift Sources/HSCC/SessionEvent.swift
             Sources/HSCC/StreamHello.swift
             scripts/model_decode_check/ThemeStub.swift
             scripts/ws_decode_check/main.swift)

echo "==> compiling ws_decode_check against shipped sources"
if ! xcrun swiftc -sdk "$(xcrun --show-sdk-path --sdk macosx)" \
    -F "$FRAMEWORK" \
    -o "$TMP/ws_decode_check" "${SWIFT_FILES[@]}" >"$TMP/compile.log" 2>&1; then
    echo "FAIL: ws_decode_check failed to compile:"
    sed 's/^/  /' "$TMP/compile.log"
    exit 2
fi

echo "==> decoding fixtures"
status=0
for f in "$FIXTURE_DIR"/*.jsonl; do
    if "$TMP/ws_decode_check" "$f"; then
        :""
    else
        status=1
    fi
done
exit $status
