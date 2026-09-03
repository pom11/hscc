#!/bin/bash
# offline_cache_fix_check.sh — prove the offline-last-known cache fix against
# the LIVE API (read-only GETs only).
#
# The fix in HSCCClient.get(path:queryItems:) now writes single-shot query
# reads under their plain path, so the offline fallback the views already wire
# up (Offline.load → .stale) has a value to show. This compiles the REAL
# HSCCClient (which contains StateCache + EndpointPath) with the real models
# and calls live read-only endpoints, then asserts the cache became populated
# under the plain path.
#
# Read-only: sessions / fleetStats / activityFeed are pure GETs.
#
# Usage:
#   scripts/offline_cache_fix_check.sh [HOST [PORT [TOKEN]]]
# Defaults to the live API derived from `hscc api status` + ~/.hscc/api-token.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
SDK=$(xcrun --sdk macosx --show-sdk-path 2>/dev/null) || {
  echo "error: no macOS SDK (is Xcode installed?)" >&2; exit 1; }

HOST="${1:-}"
PORT="${2:-}"
TOKEN="${3:-}"
if [ -z "$HOST" ] || [ -z "$PORT" ]; then
  STATUS=$(hscc api status 2>/dev/null)
  if [ -z "$HOST" ]; then
    HOST=$(echo "$STATUS" | sed -n 's/.*Listening: *\([0-9.]*\):[0-9]*.*/\1/p' | head -1)
  fi
  if [ -z "$PORT" ]; then
    PORT=$(echo "$STATUS" | sed -n 's/.*Listening: *[0-9.]*:\([0-9]*\).*/\1/p' | head -1)
  fi
fi
PORT=$(echo "$PORT" | tr -dc '0-9')
if [ -z "$TOKEN" ]; then
  TOKEN=$(tr -d '[:space:]' < ~/.hscc/api-token 2>/dev/null)
fi
if [ -z "$HOST" ] || [ -z "$PORT" ] || [ -z "$TOKEN" ]; then
  echo "error: could not determine live API host/port/token (got host='$HOST' port='$PORT' token='${TOKEN:+set}')" >&2
  exit 2
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

real_sources=(
  Sources/HSCC/HSCCClient.swift     # contains StateCache + EndpointPath + get
  Sources/HSCC/Models.swift
  Sources/Shared/SharedModels.swift
  Sources/HSCC/APIError.swift
  Sources/HSCC/SessionEvent.swift
)
harness=(
  scripts/model_decode_check/ThemeStub.swift
  scripts/offline_cache_fix_check/main.swift
)

echo "compiling real HSCCClient + models + offline-cache harness..."
if ! xcrun --sdk "$SDK" swiftc -parse-as-library -o "$TMP/check" \
     "${real_sources[@]}" "${harness[@]}" 2>"$TMP/compile.err"; then
  echo "FAIL: compile error — see below" >&2
  cat "$TMP/compile.err" >&2
  exit 1
fi

HOST="$HOST" PORT="$PORT" TOKEN="$TOKEN" "$TMP/check"
rc=$?
echo ""
if [ "$rc" = "0" ]; then
  echo "offline cache fix PASS (single-shot query reads now write last-known cache)"
else
  echo "offline cache fix FAIL (rc=$rc)"
fi
exit $rc
