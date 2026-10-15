#!/usr/bin/env bash
# actool_icon_check.sh — prove the AppIcon actually compiles and wires.
#
# WHY this exists (IOS_REDESIGN 2026-10-14): build_check.sh compiles the Swift
# targets with swiftc; it NEVER touches Assets.xcassets. A malformed AppIcon —
# wrong Contents.json, unresolvable filenames, an appearance value actool does
# not accept — compiles fine, ships, and the operator's home screen keeps the
# plain "H" placeholder (exactly the bug this whole pass set out to fix). The
# ONLY way to know the icon is real is to run the tool that consumes it.
#
# This runs actool (the asset compiler Xcode uses) and asserts TWO things:
#   1. the partial Info.plist carries CFBundleIconName == AppIcon (the key that
#      actually points the app at the icon set), and
#   2. actool emitted no "unassigned child" / "does not exist" warning (the two
#      failure modes that silently leave the icon unwired).
#
# Usage:   ios-app/scripts/actool_icon_check.sh
# Exit:    0 wired + clean, 1 missing key or warnings, 2 no actool.
set -uo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

command -v xcrun >/dev/null || { echo "error: xcrun not found (Xcode?)" >&2; exit 2; }

OUT="$(mktemp -d /tmp/actool_icon.XXXXXX)"
trap 'rm -rf "$OUT"' EXIT

LOG="$OUT/actool.log"
xcrun actool Assets.xcassets \
  --compile "$OUT" \
  --output-format human-readable-text --notices --warnings \
  --platform iphoneos --minimum-deployment-target 26.0 \
  --app-icon AppIcon \
  --output-partial-info-plist "$OUT/partial.plist" >"$LOG" 2>&1
rc=$?

fail=0
if [ "$rc" != 0 ]; then
  echo "FAIL: actool exited $rc"
  sed -n '1,20p' "$LOG"
  fail=1
fi

# 1. CFBundleIconName must be present and == AppIcon.
if plutil -p "$OUT/partial.plist" 2>/dev/null | grep -q '"CFBundleIconName" => "AppIcon"'; then
  echo "ok: CFBundleIconName => AppIcon (the app is wired to the icon set)"
else
  echo "FAIL: partial Info.plist has no CFBundleIconName => AppIcon"
  plutil -p "$OUT/partial.plist" 2>/dev/null | head -20
  fail=1
fi

# 2. No unassigned-child / missing-file warnings — the silent-unwired failure.
if grep -qE "unassigned child|does not exist" "$LOG"; then
  echo "FAIL: actool left the AppIcon set incomplete:"
  grep -E "unassigned child|does not exist" "$LOG" | sort -u | head -8
  fail=1
else
  echo "ok: no unassigned children / missing files"
fi

# 3. actool derived the device sizes (proof it accepted the single-size master).
if [ -f "$OUT/AppIcon60x60@2x.png" ]; then
  echo "ok: derived sizes generated (single-size master accepted)"
else
  echo "FAIL: actool generated no derived icon sizes"
  fail=1
fi

if [ "$fail" = 0 ]; then
  echo "ACTOOL ICON CHECK PASSED — AppIcon compiles and is wired (still not verified on a device)."
  exit 0
else
  exit 1
fi
