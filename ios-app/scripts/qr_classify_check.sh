#!/bin/bash
# Prove QRPairing.classify maps every HSCCError to the right actionable
# QRPairingOutcome (t_cf296e48). Compiles the REAL pieces into a macOS CLI:
#   - Sources/HSCC/APIError.swift      (the real HSCCError enum)
#   - a slice of SetupQRCode.swift holding real QRPairingOutcome +
#     QRPairing.classify, with the `test` method removed (it drags in
#     HSCCClient/URLSession, which is out of scope for a classification check).
# This is the exact logic the Settings connect step and onboarding path now use.
#
# Usage: bash scripts/qr_classify_check.sh   (passes on macOS with Xcode CLT)

set -u
cd "$(dirname "$0")/.." || exit 1
ROOT="$(pwd)"
SDK="${SDK:-macosx}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

error() { echo "error: $*" >&2; exit 1; }

echo "slicing the real HSCCError + QRPairingOutcome + QRPairing.classify..."

# 1) Real HSCCError.
api_src="Sources/HSCC/APIError.swift"
cp "$api_src" "$TMP/APIError.swift"

# 2) Slice SetupQRCode.swift: start at the QRPairingOutcome doc comment, run to
#    the end of the file, but DROP the `test` method (HSCCClient-dependent).
setup_src="Sources/HSCC/SetupQRCode.swift"
if ! grep -q '^enum QRPairingOutcome: Equatable {' "$setup_src" \
   || ! grep -q 'static func classify(_ error: Error)' "$setup_src"; then
  error "could not locate the real QRPairingOutcome/classify markers (moved?)"
fi
awk '
  /^\/\/\/ The outcome of a completed QR pairing attempt/ { inc=1 }
  inc { print }
' "$setup_src" > "$TMP/SetupQRCode.raw.swift"
# Remove the `test` method block: from its `@MainActor` attribute or
# `static func test(host:` line to the preceding `    }` closing brace, so the
# dangling actor attribute never leaks onto classify.
awk '
  /static func test\(host:/ { skip=1; print ""; next }
  skip && /^    \}$/ { skip=0; next }
  skip { next }
  /^    @MainActor$/ { next }
  { print }
' "$TMP/SetupQRCode.raw.swift" > "$TMP/SetupQRCode.swift"

echo "compiling the REAL classify logic + harness into a macOS CLI..."
real_sources=("$TMP/APIError.swift" "$TMP/SetupQRCode.swift")
if ! xcrun --sdk "$SDK" swiftc -o "$TMP/qr_classify_check" \
     "${real_sources[@]}" scripts/qr_classify_check/main.swift 2>"$TMP/compile.err"; then
  echo "error: failed to compile qr_classify_check — see below" >&2
  cat "$TMP/compile.err" >&2
  exit 1
fi

"$TMP/qr_classify_check"
exit $?
