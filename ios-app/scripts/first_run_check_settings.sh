#!/bin/bash
# first_run_check_settings.sh — PROVE the connection-state / re-probe trigger
# logic of SettingsStore headlessly.
#
# Card t_7f699e3c: "AUDIT the settings/first-run surface — make the app's
# state honest and self-explanatory." Companion to first_run_check.sh, which
# covers the QR DECODE path; this one covers the SETTINGS-STORE path the banner
# and the connect/save steps depend on.
#
# The central defect this locks in: the root connection banner was keyed on
# SettingsStore.isConfigured, which stays `true` when an already-configured
# cluster's token is swapped for a wrong one, or its host is changed to an
# unreachable address. So after a silent bad edit, the banner kept showing the
# last successful ping (stale-green "Connected") while the stored config was
# unusable — the operator was told success while nothing worked.
#
# Fix: SettingsStore.connectionIdentity (host|port|token-hash) changes in every
# one of those cases, and ContentView now re-probes on it. This harness compiles
# the REAL SettingsStore + KeychainStore + a SharedModels slice (never
# redeclared) into a plain macOS CLI and asserts:
#   * isConfigured stays true across a wrong-token swap and a host change
#     (reproduces the stale-green bug condition);
#   * connectionIdentity changes in every one of those cases (so the banner can
#     re-probe exactly when it must);
#   * re-saving identical state leaves identity unchanged (no spurious re-probe).
#
# Note: settings.appGroupUnavailable is a DEVICE-ONLY signal (an absent
# App-Group entitlement / unprovisioned container makes
# `FileManager.containerURL(forSecurityApplicationGroupIdentifier:)` return
# nil). macOS auto-creates group containers, so the flag reads as "available"
# and is NOT asserted here.
#
# Usage: scripts/first_run_check_settings.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
SDK=$(xcrun --sdk macosx --show-sdk-path 2>/dev/null) || {
  echo "error: no macOS SDK (is Xcode installed?)" >&2; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# Compile the REAL sources — the same files that ship in the app, never
# redeclared in the harness (SharedModels is sliced first below to drop a
# UIKit-only helper). Any of these markers moving should fail loudly rather
# than silently test the wrong thing.
for marker in \
  "Sources/Shared/SharedModels.swift" \
  "Sources/HSCC/KeychainStore.swift" \
  "Sources/HSCC/SettingsStore.swift" \
  "Sources/HSCC/StateCache.swift"; do
  if [[ ! -f "$marker" ]]; then
    echo "error: missing source $marker (markers moved?)" >&2
    exit 1
  fi
done

# Slice the REAL SharedModels minus its `ClusterState.color` / and
# `TopologyNode.NodeState.color` extensions, which reference Theme.Semantic ->
# UIColor (UIKit) and would break a macOS CLI. The colour helpers are
# view-layer convenience, not part of the settings logic under test. Keep
# everything else (SavedCluster, AppGroup, KeychainShared, APIConfig,
# KeychainConstants, SnapshotStore, TopologyNode) intact.
shared_src="Sources/Shared/SharedModels.swift"
if ! grep -q 'enum ClusterState: String {' "$shared_src"; then
  echo "error: could not locate ClusterState in SharedModels (markers moved?)" >&2
  exit 1
fi
# Delete every `var color: Color { ... }` block (brace-balanced, works at any
# indent). The colour helpers reference Theme.Semantic -> UIColor, which a plain
# macOS swiftc CLI cannot see.
python3 - "$shared_src" "$TMP/SharedModels.swift" <<'PY'
import sys
src, dst = sys.argv[1], sys.argv[2]
skip = 0
out = []
with open(src) as f:
    for line in f:
        if skip:
            # Track brace depth so we drop the WHOLE block, not just the first
            # line, regardless of indentation.
            skip += line.count('{') - line.count('}')
            if skip <= 0:
                skip = 0
            continue
        if line.strip().startswith('var color: Color {'):
            skip = line.count('{') - line.count('}')  # >0 unless inline-{}; covered below
            if skip <= 0:
                skip = 1
            continue
        out.append(line)
with open(dst, 'w') as f:
    f.writelines(out)
PY

real_sources=(
  "$TMP/SharedModels.swift"
  "Sources/HSCC/KeychainStore.swift"
  "Sources/HSCC/SettingsStore.swift"
  "Sources/HSCC/StateCache.swift"
)
harness=(scripts/first_run_check_settings/main.swift)

echo "compiling the REAL SettingsStore + harness into a macOS CLI..."
if ! xcrun --sdk "$SDK" swiftc -o "$TMP/first_run_check_settings" \
     "${real_sources[@]}" "${harness[@]}" 2>"$TMP/compile.err"; then
  echo "error: failed to compile the settings-store check — see below" >&2
  cat "$TMP/compile.err" >&2
  exit 1
fi

"$TMP/first_run_check_settings"
exit $?
