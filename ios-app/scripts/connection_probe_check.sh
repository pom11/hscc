#!/bin/bash
# connection_probe_check.sh — PROVE the probe state machine that fixes the
# drawer pill stuck at "checking…" forever (t_e5d24c6d, operator field report
# v2.9.0: API up, pill never green — nothing ever probes while status == .unknown).
#
# Like connection_banner_check.sh / fleet_models_check.sh, this compiles the
# REAL source — Sources/HSCC/ConnectionMonitor.swift (never redeclared here),
# plus the models its classification touches — together with a harness
# (scripts/connection_probe_check/main.swift) into a plain macOS CLI, then
# asserts the contract the fix must obey:
#   * the probe endpoint stays "/v1/health" (the confirmed handle_health route);
#   * the committed live capture of /v1/health still decodes into HealthResponse
#     — decode drift would silently re-strand the pill, so it fails HERE;
#   * classification keeps the file's any-HTTP-response-is-reachability rule,
#     with the ONE operator-ruled exception (401/403/"unauthorized" = auth
#     refusal, surfaced as .unreachable with a token-specific reason);
#   * the FIRST real probe result flips .unknown (the field bug);
#   * a failed probe NEVER pins the banner — a later real success clears it
#     (the t_4889e978 no-pin contract survives the new probe).
#
# The probe's network call is INJECTED, so this gate is fully hermetic: no
# socket is opened (the live API is never contacted from a gate).
#
# NEGATIVE TEST (guarded): break ONE classification rule deliberately and
# confirm the harness FAILS. A harness that cannot fail is worthless:
#   export CONNECTION_PROBE_NEGATIVE_TEST=1
#
# Usage: scripts/connection_probe_check.sh
set -uo pipefail
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
cd "$(dirname "${BASH_SOURCE[0]}")/.."

export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
SDK=$(xcrun --sdk macosx --show-sdk-path 2>/dev/null) || {
  echo "error: no macOS SDK (is Xcode installed?)" >&2; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

real_monitor="Sources/HSCC/ConnectionMonitor.swift"
real_models=(
  Sources/HSCC/Models.swift
  Sources/Shared/SharedModels.swift
  Sources/HSCC/APIError.swift
)
harness="scripts/connection_probe_check/main.swift"
fixture_dir="scripts/connection_probe_check/fixtures"
# The colours SharedModels.swift references render nothing on macOS but the
# types must exist — the same one-file shim model_decode_check uses.
theme_stub="scripts/model_decode_check/ThemeStub.swift"

for f in "$real_monitor" "$harness" "$theme_stub" "$fixture_dir/v1_health.json"; do
  if [ ! -f "$f" ]; then echo "error: $f missing — did it get moved?" >&2; exit 1; fi
done
# The harness must drive the REAL monitor/probe; if these markers vanish it is
# testing a copy, which is the false-green these harnesses exist to prevent.
if ! grep -q "ConnectionProbe.runReachabilityProbe" "$harness" ||
   ! grep -q "ConnectionProbe.classify" "$harness"; then
  echo "error: harness no longer drives the real probe (markers moved?)" >&2; exit 1
fi
if ! grep -q "Sources/HSCC/ConnectionMonitor.swift" "$SELF"; then
  echo "error: runner no longer compiles the real monitor (marker moved?)" >&2; exit 1
fi

echo "compiling the REAL ConnectionMonitor + models + harness into a macOS CLI..."
if ! xcrun --sdk "$SDK" swiftc -o "$TMP/connection_probe_check" \
     "$real_monitor" "${real_models[@]}" "$theme_stub" "$harness" 2>"$TMP/compile.err"; then
  echo "error: failed to compile the connection-probe state machine — see below" >&2
  cat "$TMP/compile.err" >&2
  exit 1
fi
if [ -s "$TMP/compile.err" ]; then
  echo "warning: compiler output while building the harness:"; cat "$TMP/compile.err"
fi

FIXDIR=$(cd "$fixture_dir" && pwd)

if [ "${CONNECTION_PROBE_NEGATIVE_TEST:-0}" = "1" ]; then
  # Prove the harness can fail: copy the real monitor, sabotage ONE rule (drop
  # the "unauthorized" machine-slug arm of classify), and require the harness
  # to report failures.
  echo "NEGATIVE TEST: dropping the unauthorized-slug arm and requiring the harness to fail..."
  mkdir -p "$TMP/sabotage"
  if ! sed -e 's/|| code == "unauthorized"//' \
       "$real_monitor" > "$TMP/sabotage/ConnectionMonitor.swift"; then
    echo "error: negative-test sed failed — the sabotage anchor moved" >&2; exit 1
  fi
  if ! cmp -s "$real_monitor" "$TMP/sabotage/ConnectionMonitor.swift"; then
    if xcrun --sdk "$SDK" swiftc -o "$TMP/sabotaged" \
         "$TMP/sabotage/ConnectionMonitor.swift" "${real_models[@]}" "$theme_stub" "$harness" \
         2>"$TMP/sab.err"; then
      if "$TMP/sabotaged" "$FIXDIR" >"$TMP/sab.out" 2>&1; then
        echo "NEGATIVE TEST FAILED: the harness passed with the auth-slug rule removed — it is not load-bearing" >&2
        exit 1
      fi
      echo "negative test OK: the sabotaged classifier was caught (harness exited non-zero)"
      grep -m3 "^FAIL" "$TMP/sab.out" | sed 's/^/    /'
      exit 0
    fi
    echo "error: sabotaged variant did not compile — anchor drift, fix this script" >&2
    cat "$TMP/sab.err" >&2
    exit 1
  fi
  echo "error: negative test sed changed nothing (anchor drift)" >&2
  exit 1
fi

"$TMP/connection_probe_check" "$FIXDIR"
rc=$?
if [ "$rc" = "0" ]; then
  echo "connection-probe gate green (hermetic — run CONNECTION_PROBE_NEGATIVE_TEST=1 to prove it can fail)"
fi
exit $rc
