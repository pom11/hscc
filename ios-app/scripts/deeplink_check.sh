#!/bin/bash
# deeplink_check.sh — PROVE the hscc:// deep-link router handles malformed and
# stale links honestly (never crashes, never lands on a blank screen).
#
# Card t_5320945e: the deep-link router shipped in 8c400d8 (t_136762f3, four
# commits) with NO headless harness; with no iOS runtime on the review host a
# resolution bug reached the operator's device untested.
#
# Like connection_banner_check.sh, this compiles the REAL source —
# Sources/HSCC/DeepLink.swift (never redeclared here) — together with tiny
# stubs for the iOS-only app types it touches (ContentView.Tab,
# ProjectDetailView.Section, HSCCClient) and a pure logic harness
# (scripts/deeplink_check/main.swift) into a plain macOS CLI, then asserts:
#   * valid project / card / session links resolve to the right destination;
#   * an unknown host or scheme is REJECTED, not silently opened;
#   * a well-formed link to a NON-EXISTENT project/card still lands on a real
#     destination — never blank, never a crash;
#   * malformed / truncated URLs do not crash;
#   * percent-encoded and unicode identifiers round-trip;
#   * a link arriving before settings are configured behaves sanely.
#
# NEGATIVE TEST (guarded): break the router deliberately and confirm this
# harness FAILS, then restore. A harness that cannot fail is worthless.
#   export DEEPLINK_NEGATIVE_TEST=1
#
# Usage: scripts/deeplink_check.sh
set -uo pipefail
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
cd "$(dirname "${BASH_SOURCE[0]}")/.."

export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
SDK=$(xcrun --sdk macosx --show-sdk-path 2>/dev/null) || {
  echo "error: no macOS SDK (is Xcode installed?)" >&2; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

real_source="Sources/HSCC/DeepLink.swift"
if [ ! -f "$real_source" ]; then
  echo "error: $real_source missing — did the router get moved?" >&2; exit 1
fi

stubs="scripts/deeplink_check/Stubs.swift"
harness="scripts/deeplink_check/main.swift"
# The harness must call the REAL router; if that marker ever vanishes the
# harness is testing a copy, not the shipped code.
if ! grep -q "DeepLinkRouter.shared" "$harness"; then
  echo "error: harness no longer references the shared router (marker moved?)" >&2; exit 1
fi
if ! grep -q "Sources/HSCC/DeepLink.swift" "$SELF"; then
  echo "error: runner no longer compiles the real router (marker moved?)" >&2; exit 1
fi

echo "compiling the REAL DeepLinkRouter + stubs + harness into a macOS CLI..."
if ! xcrun --sdk "$SDK" swiftc -o "$TMP/deeplink_check" \
     "$real_source" "$stubs" "$harness" 2>"$TMP/compile.err"; then
  echo "error: failed to compile the deep-link router harness — see below" >&2
  cat "$TMP/compile.err" >&2
  exit 1
fi

# ---- NEGATIVE TEST: prove the harness can actually FAIL. -------------------
# Deliberately sabotage one behaviour the harness asserts (REJECT unknown
# hosts), run the harness, and require a non-zero exit. A guard that cannot
# fail is worthless.
if [ "${DEEPLINK_NEGATIVE_TEST:-0}" = "1" ]; then
  echo ""
  echo "== NEGATIVE TEST: breaking the router to confirm the harness FAILS =="
  NEGROOT="$TMP/neg"
  mkdir -p "$NEGROOT"
  # Deterministic sabotage: rewrite the parser's unknown-host DEFAULT branch so
  # it SILENTLY routes to a guessed project instead of rejecting. The harness's
  # "unknown host must be rejected" assertions must then trip and fail.
  python3 - "$real_source" "$NEGROOT/DeepLink_broken.swift" <<'PY'
import sys
src, dst = sys.argv[1], sys.argv[2]
s = open(src).read()
old = 'default:\n            return .invalid(reason: "HSCC doesn\'t understand a \'\\(kind)\' link.")'
new = 'default:\n            return .project(name: target)  // SABOTAGED: silent guessed route'
if old not in s:
    raise SystemExit("NEGATIVE TEST ERROR: couldn't find the unknown-host default branch to break")
assert new not in s, "already sabotaged"
open(dst, 'w').write(s.replace(old, new))
PY
  if ! xcrun --sdk "$SDK" swiftc -o "$TMP/deeplink_neg" \
       "$NEGROOT/DeepLink_broken.swift" "$stubs" "$harness" 2>"$TMP/neg.err"; then
    echo "error: negative-test fixture failed to compile" >&2
    cat "$TMP/neg.err" >&2
    exit 1
  fi
  if "$TMP/deeplink_neg" >"$TMP/neg.out" 2>&1; then
    echo "NEGATIVE TEST FAILED: sabotaged router still passed — the harness cannot fail!" >&2
    echo "------ sabotaged harness output ------" >&2
    cat "$TMP/neg.out" >&2
    exit 1
  fi
  echo "NEGATIVE TEST PASSED: sabotaged router produced failing assertions."
  echo "(A harness that cannot fail is worthless — this one can.)"
  # Leave a marker so the reviewing operator can see exactly what was sabotaged.
  grep -n "silently route\|\.project(name: target)" "$NEGROOT/DeepLink_broken.swift" | head -3
  echo ""
  echo "== now running the UNTOUCHED router harness =="
fi

"$TMP/deeplink_check"
exit $?
