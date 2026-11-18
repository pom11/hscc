#!/usr/bin/env bash
# actool_icon_check.sh — prove the macOS asset catalog actually compiles and
# that AppIcon resolves. build_check.sh is swiftc-only and never sees an asset
# catalog, so without this a broken icon set would ship silently.
#
# Usage: macos-app/scripts/actool_icon_check.sh
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

out=$(xcrun actool Sources/HSCCMac/Assets.xcassets \
        --compile "$TMP" \
        --platform macosx \
        --minimum-deployment-target 14.0 \
        --app-icon AppIcon \
        --output-partial-info-plist "$TMP/partial.plist" \
        --warnings 2>&1)
w=$(printf '%s\n' "$out" | grep -c "warning:" || true)
if [ "$w" != "0" ]; then
  printf '%s\n' "$out" | grep "warning:" | head -20
  echo "FAIL: $w actool warning(s)"
  exit 1
fi

icon=$(plutil -extract CFBundleIconName raw "$TMP/partial.plist")
[ "$icon" = "AppIcon" ] || { echo "FAIL: CFBundleIconName is '${icon:-<missing>}', expected AppIcon" >&2; exit 1; }
[ -s "$TMP/AppIcon.icns" ] || { echo "FAIL: actool did not emit AppIcon.icns" >&2; exit 1; }
[ -s "$TMP/Assets.car" ] || { echo "FAIL: actool did not emit Assets.car" >&2; exit 1; }

echo "actool clean: AppIcon resolves (CFBundleIconName=AppIcon), 0 warnings"
