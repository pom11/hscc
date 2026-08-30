#!/bin/bash
# first_run_check.sh — PROVE the QR-scan -> settings -> test-connection path.
#
# Card t_e118313c: "AUDIT: first-run flow — QR scan to first working chat, find
# anything that strands the operator."
#
# The highest-value dead-end found on that path was in the connection/settings
# layer: SetupQRCode.decode accepted a whitespace-only token (the manual-entry
# form trimmed its token, the QR parser did not), so a scanned QR with a
# whitespace token applied cleanly and then 401'd on EVERY request with no
# in-app explanation — the app reported "configured" while nothing worked.
#
# Like model_decode_check.sh, this compiles the REAL source — SetupQRCode.swift
# (never redeclared here) — together with a pure-logic harness
# (scripts/first_run_check/main.swift) into a plain macOS CLI, then asserts:
#   * the canonical payload decodes;
#   * a whitespace-only token is REJECTED (invalid payload, never applied);
#   * an empty token is REJECTED;
#   * a token with surrounding whitespace decodes (it is trimmed at the
#     SettingsView.save choke point before it ever reaches the Keychain);
#   * wrong-schema / truncated / wrong-version payloads each fail with the
#     right explanatory error, never a silent nothing.
#
# The parser is pure Foundation + Decodable, and the harness is pure logic, so
# a macOS CLI is the faithful runner — there is no iOS platform runtime on this
# host, so a runtime claim is never made here (same pattern as the other
# *_check.sh harnesses).
#
# Usage: scripts/first_run_check.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
SDK=$(xcrun --sdk macosx --show-sdk-path 2>/dev/null) || {
  echo "error: no macOS SDK (is Xcode installed?)" >&2; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# Slice the REAL parser — SetupQRCode + SetupQRCodeError — out of
# SetupQRCode.swift so we test the exact decode/validate logic used by the app,
# not a redeclaration. The rest of that file (QRPairing) references
# HSCCClient/HSCCError and drives the CONNECTION side, which is out of scope
# for a decode check and would drag in URLSession. Marker must exist.
setup_src="Sources/HSCC/SetupQRCode.swift"
if ! grep -q '^struct SetupQRCode: Decodable, Equatable {' "$setup_src" \
   || ! grep -q '^enum SetupQRCodeError: LocalizedError, Equatable {' "$setup_src"; then
  echo "error: could not locate the real SetupQRCode parser (markers moved?)" >&2
  exit 1
fi
awk '/^\/\/\/ The outcome of a completed QR pairing attempt/{exit}
     {print}' \
  "$setup_src" > "$TMP/SetupQRCode.swift"
# Bound the slice: stop right before the QRPairingOutcome doc comment so the
# connection-layer enums never leak into the decode check.

real_sources=("$TMP/SetupQRCode.swift")
harness=(scripts/first_run_check/main.swift)

echo "compiling the REAL SetupQRCode + harness into a macOS CLI..."
if ! xcrun --sdk "$SDK" swiftc -o "$TMP/first_run_check" \
     "${real_sources[@]}" "${harness[@]}" 2>"$TMP/compile.err"; then
  echo "error: failed to compile the first-run check — see below" >&2
  cat "$TMP/compile.err" >&2
  exit 1
fi

"$TMP/first_run_check"
exit $?
