#!/bin/bash
# reconnect_check.sh — PROVE the reconnect guarantee of SessionStreamCursor.
#
# Card t_218cb9ec: "Every event carries a sequence number; on reconnect the
# client requests everything after the last it saw. Prove: kill the connection
# mid-stream, reconnect, and the transcript has no gap and no repeat."
#
# Like model_decode_check.sh, this compiles the REAL source —
# Sources/HSCC/SessionStreamCursor.swift (never redeclared here) — together with
# a harness (scripts/reconnect_check/main.swift) into a plain macOS CLI, then
# runs six scenarios that cut the stream mid-way, reconnect with the cursor's
# resumeRequest, and assert the transcript is exactly the producer's events once
# each. A failure means the real reconnect logic no longer holds its guarantee.
#
# The cursor is pure Foundation and the harness is pure logic, so a macOS CLI
# is the faithful runner — there is no iOS platform runtime on this host, and a
# runtime claim is never made here. This follows the exact pattern of
# model_decode_check.sh so the repo has ONE consistent way to prove logic.
#
# Usage: scripts/reconnect_check.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
SDK=$(xcrun --sdk macosx --show-sdk-path 2>/dev/null) || {
  echo "error: no macOS SDK (is Xcode installed?)" >&2; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

real_sources=(
  Sources/HSCC/SessionStreamCursor.swift
)
harness=(
  scripts/reconnect_check/main.swift
)

echo "compiling the REAL SessionStreamCursor + harness into a macOS CLI..."
if ! xcrun --sdk "$SDK" swiftc -o "$TMP/reconnect_check" \
     "${real_sources[@]}" "${harness[@]}" 2>"$TMP/compile.err"; then
  echo "error: failed to compile the reconnect cursor — see below" >&2
  cat "$TMP/compile.err" >&2
  exit 1
fi

"$TMP/reconnect_check"
exit $?
