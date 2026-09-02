#!/bin/bash
# fleet_offline_check.sh — prove the REAL Offline.load semantics FleetView now
# depends on: a failed fetch WITH a held/cached value yields .stale (last known),
# NOT .failed; with nothing it yields .failed. Compiles the real LoadState.swift
# (the Offline enum) against a minimal HSCCClient/HSCCError stub.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
SDK=$(xcrun --sdk macosx --show-sdk-path 2>/dev/null) || {
  echo "error: no macOS SDK (is Xcode installed?)" >&2; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

echo "compiling real LoadState.swift (Offline enum) + offline semantics harness..."
if ! swiftc -sdk "$SDK" -parse-as-library \
      Sources/HSCC/Views/LoadState.swift \
      scripts/fleet_offline_check/main.swift \
      -o "$TMP/check" 2>&1; then
  echo "FAIL: compile error"
  exit 1
fi

"$TMP/check"
rc=$?
echo ""
if [ "$rc" = "0" ]; then
  echo "fleet offline semantics PASS (Offline.load yields .stale on cached failure, .failed on nothing)"
else
  echo "fleet offline semantics FAIL"
fi
exit $rc
