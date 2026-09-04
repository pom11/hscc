#!/bin/bash
# shared_store_check.sh — the App Group contract, checked statically.
#
# The operator hit this on a real device:
#   Couldn't read values in CFPrefsPlistSource (Domain: group.com.hscc.ios ...)
# A free personal Apple team cannot provision an App Group container, so the
# shared suite silently does not exist.
#
# WHAT CAN AND CANNOT BE PROVEN HERE. `containerURL(forSecurityApplicationGroup
# Identifier:)` returns a URL on macOS even for an UNREGISTERED group, so the
# runtime detection is a device-only signal (SettingsStore documents this).
# What IS checkable is the contract around it, which is what actually keeps a
# broken App Group from silently stranding the widget and intents:
#
#   1. The app MAY fall back to .standard so it keeps working, but
#   2. that fallback must never be SILENT — unavailability has to be surfaced, and
#   3. the extensions must read the shared suite WITHOUT a fallback, so they
#      fail visibly rather than reading the app's private defaults.
#
# An earlier version of this script asserted a `SharedStore` gate that the
# implementation no longer uses; it failed on correct code. It now checks the
# design that is actually in place.
#
# Usage: ios-app/scripts/shared_store_check.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
rc=0
ok()   { echo "  ok: $1"; }
fail() { echo "FAIL: $1"; rc=1; }

SETTINGS=Sources/HSCC/SettingsStore.swift
SHARED=Sources/Shared/SharedModels.swift
CONTENT=Sources/HSCC/ContentView.swift

# 1. Unavailability is DETECTED.
grep -q "containerURL(forSecurityApplicationGroupIdentifier:" "$SETTINGS" \
  && ok "app detects a missing App Group container" \
  || fail "no App Group container detection in $SETTINGS"

# 2. It is SURFACED as observable state, not swallowed.
grep -q "@Published private(set) var appGroupUnavailable" "$SETTINGS" \
  && ok "unavailability is published observable state" \
  || fail "appGroupUnavailable is not a published property"

# 3. The UI actually reads that state (a flag nothing renders is not surfacing).
grep -q "settings.appGroupUnavailable" "$CONTENT" \
  && ok "UI renders the unavailable state" \
  || fail "nothing in ContentView reads appGroupUnavailable"

# 4. The EXTENSIONS must not fall back to .standard — they would silently read
#    the app's private defaults and look configured while being wrong.
if grep -n "UserDefaults(suiteName:" "$SHARED" | grep -q "?? .standard"; then
  fail "shared/extension path falls back to .standard (would hide a broken group)"
else
  ok "extension path reads the shared suite with NO fallback"
fi

# 5. The extension path must degrade to nil rather than fabricate defaults.
grep -q "static func load() -> APIConfig?" "$SHARED" \
  && ok "APIConfig.load is optional (honest nil when unconfigured)" \
  || fail "APIConfig.load does not return an optional"

echo ""
if [ "$rc" -eq 0 ]; then
  echo "SHARED STORE CHECK PASSED — a broken App Group cannot fail silently"
else
  echo "SHARED STORE CHECK FAILED — see above"
fi
exit $rc
