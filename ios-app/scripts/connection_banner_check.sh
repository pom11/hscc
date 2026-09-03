#!/bin/bash
# connection_banner_check.sh — PROVE the connection banner's honest state machine.
#
# Card t_4889e978: the banner must reflect the MOST RECENT REAL API outcome,
# not a stale one-shot launch probe. A successful request clears it; a real
# (transport) failure sets it; a request merely in flight is never an error.
#
# Like chat_state_check.sh / reconnect_check.sh, this compiles the REAL source —
# Sources/HSCC/ConnectionMonitor.swift (never redeclared here) — with a pure
# logic harness (scripts/connection_banner_check/main.swift) into a plain macOS
# CLI, then asserts:
#   * fail -> success CLEARS the alarm (back to .reachable);
#   * success -> fail SETS it (back to .unreachable);
#   * in-flight is not an error — the monitor only raises red on a COMPLETED
#     transport failure, never while a request is underway.
#
# Usage: scripts/connection_banner_check.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
SDK=$(xcrun --sdk macosx --show-sdk-path 2>/dev/null) || {
  echo "error: no macOS SDK (is Xcode installed?)" >&2; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

real_source="Sources/HSCC/ConnectionMonitor.swift"
if [ ! -f "$real_source" ]; then
  echo "error: $real_source missing — did the monitor get moved?" >&2
  exit 1
fi

harness="scripts/connection_banner_check/main.swift"
if ! grep -q "ConnectionMonitor.shared" "$harness"; then
  echo "error: harness no longer references the shared monitor (marker moved?)" >&2
  exit 1
fi

echo "compiling the REAL ConnectionMonitor + harness into a macOS CLI..."
if ! xcrun --sdk "$SDK" swiftc -o "$TMP/connection_banner_check" \
     "$real_source" "$harness" 2>"$TMP/compile.err"; then
  echo "error: failed to compile the connection banner state machine — see below" >&2
  cat "$TMP/compile.err" >&2
  exit 1
fi

"$TMP/connection_banner_check"
exit $?
