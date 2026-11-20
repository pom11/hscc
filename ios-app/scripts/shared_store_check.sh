#!/bin/bash
# shared_store_check.sh — the App Group contract, checked statically.
#
# ORIGIN, AND THE CORRECTION. This started from a device console line:
#   Couldn't read values in CFPrefsPlistSource (Domain: group.com.hscc.ios ...)
#   Using kCFPreferencesAnyUser with a container is only allowed for System
#   Containers
# It was first read as proof that a free personal Apple team cannot provision
# the App Group. That conclusion was WRONG. On 2026-09-07 the operator checked
# the device with the group-unavailable banner shipped and wired: NO banner.
# The container is provisioned and the shared suite works. That cfprefsd line
# is common benign noise and is NOT evidence of a broken App Group.
#
# The reliable signal is containerURL(forSecurityApplicationGroupIdentifier:),
# which returns nil on iOS when the group is genuinely missing — that is what
# SettingsStore checks and what the banner reflects. Do not re-diagnose this
# from console output alone.
#
# The contract below is still worth enforcing: it is what makes a REAL App
# Group failure loud instead of silently stranding the widget and intents.
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
# The shared stores live under Sources/Shared/ (SharedStore.swift holds the App
# Group identifier + the typed stores, SharedModels.swift the wire payloads).
# These two asserts are deliberately directory-scoped, not file-scoped: the rule
# is about ANY shared/extension code, and a file-scoped grep silently stops
# covering the rule the moment a type moves between files (it moved once, to
# SharedStore.swift, on 2026-11-17).
SHARED_DIR=Sources/Shared
CONTENT=Sources/HSCC/Views/ConfigView.swift

# 1. Unavailability is DETECTED — by the real containerURL check, which now
#    lives once in Sources/Shared/SharedStore.swift and is consumed by
#    SettingsStore (a second inline copy is how the two drift apart).
if grep -rq "containerURL(forSecurityApplicationGroupIdentifier:" \
     "$SETTINGS" "$SHARED_DIR"; then
  ok "app detects a missing App Group container"
else
  fail "no App Group container detection in $SETTINGS / $SHARED_DIR"
fi

# 2. It is SURFACED as observable state, not swallowed.
grep -q "@Published private(set) var appGroupUnavailable" "$SETTINGS" \
  && ok "unavailability is published observable state" \
  || fail "appGroupUnavailable is not a published property"

# 3. The UI actually reads that state (a flag nothing renders is not surfacing).
grep -q "settings.appGroupUnavailable" "$CONTENT" \
  && ok "UI renders the unavailable state" \
  || fail "nothing in ConfigView reads appGroupUnavailable"

# 4. The EXTENSIONS must not fall back to .standard — they would silently read
#    the app's private defaults and look configured while being wrong.
if grep -rn "UserDefaults(suiteName:" "$SHARED_DIR" | grep -q "?? .standard"; then
  fail "shared/extension path falls back to .standard (would hide a broken group)"
else
  ok "extension path reads the shared suite with NO fallback"
fi

# 5. The extension path must degrade to nil rather than fabricate defaults.
grep -rq "static func load() -> APIConfig?" "$SHARED_DIR" \
  && ok "APIConfig.load is optional (honest nil when unconfigured)" \
  || fail "APIConfig.load does not return an optional"

# 6. The group is DERIVED, not named: one prefix in project.yml, one Info.plist
#    key per target, and the shared Swift reads that key. A literal creeping
#    back into Swift would silently pin a custom-prefix build to com.hscc.
grep -q 'HSCC_APP_GROUP: group\.$(HSCC_BUNDLE_PREFIX)\.ios' project.yml \
  && ok "project.yml derives the App Group from HSCC_BUNDLE_PREFIX" \
  || fail "project.yml does not derive HSCC_APP_GROUP from HSCC_BUNDLE_PREFIX"
for plist in Sources/Info.plist Sources/HSCCWidgets/Info.plist \
             Sources/HSCCLiveActivityMonitor/Info.plist; do
  grep -q 'HSCCAppGroup' "$plist" \
    || fail "$plist does not carry the HSCCAppGroup key"
done
ok "all three iOS Info.plists carry HSCCAppGroup"
if grep -q 'group\.com\.hscc\.ios' "$SHARED_DIR/SharedStore.swift"; then
  fail "SharedStore.swift still names the App Group literal"
else
  ok "SharedStore.swift reads the group from Info.plist (no literal)"
fi

echo ""
if [ "$rc" -eq 0 ]; then
  echo "SHARED STORE CHECK PASSED — a broken App Group cannot fail silently"
else
  echo "SHARED STORE CHECK FAILED — see above"
fi
exit $rc
