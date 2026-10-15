#!/usr/bin/env bash
# make_appicon.sh — render AppIcon.appiconset PNGs from assets-master/*.svg.
#
# The icon is designed as SVG (assets-master/appicon-{dark,light,tinted}.svg),
# approved as candidate A "Orbit" in the IOS_REDESIGN_2026-10-14 pass. Xcode
# needs rasterised PNGs in the asset catalog, and hand-dropping them is not
# reproducible — this script is the ONLY sanctioned way the PNGs are made.
#
# Format: the single-size (Xcode 15+/iOS 17+) THREE-appearance form that ships
# in Xcode 27's own iOS app template (verified against
#   .../Templates/Project Templates/MultiPlatform/Application/
#      iOS Foveated Streaming App.xctemplate/Assets.xcassets/AppIcon.appiconset
# ): one 1024px universal image per appearance —
#   AppIcon.png          default   (what a light home screen shows)
#   AppIcon-dark.png     dark      (appearance: luminosity=dark)
#   AppIcon-tinted.png   tinted    (appearance: luminosity=tinted)
# iOS derives every device size itself; there is no 2x/3x lottery to keep
# in sync.
#
# TRAP discovered the hard way (actool from Xcode 27):
#   * an appearance value of "light" is NOT accepted for an app icon set — the
#     child is left "unassigned" and the icon silently does not wire. The light
#     appearance is the DEFAULT entry (no `appearances` key). dark and tinted
#     take explicit appearance entries.
#   * every PNG must sit at the TOP LEVEL of AppIcon.appiconset; actool does
#     not resolve subfolder paths ("file ... does not exist").
#   * the classic full-size set also fails to assign under a CLI actool run —
#     do not "fix" this by adding 60x60@2x etc.
# Gate it with scripts/actool_icon_check.sh (build_check.sh is swiftc-only and
# never sees the catalog at all).
#
# Requires: rsvg-convert (brew install librsvg), sips.
# Usage:    ios-app/scripts/make_appicon.sh
# Exit:     0 wrote all PNGs, 1 missing tool/master/bad output.
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

command -v rsvg-convert >/dev/null || { echo "error: rsvg-convert not found (brew install librsvg)" >&2; exit 1; }

SET="Assets.xcassets/AppIcon.appiconset"
rm -rf "$SET"
mkdir -p "$SET"

render() {  # render <master-svg> <out-png>
  local src="$1" out="$2"
  [ -f "$src" ] || { echo "error: missing master $src" >&2; exit 1; }
  rsvg-convert -w 1024 -h 1024 "$src" -o "$out"
  local dim; dim=$(sips -g pixelWidth -g pixelHeight "$out" | awk '/pixelWidth/{w=$2}/pixelHeight/{h=$2}END{print w"x"h}')
  [ "$dim" = "1024x1024" ] || { echo "error: $out is $dim, expected 1024x1024" >&2; exit 1; }
  local sz; sz=$(stat -f%z "$out")
  [ "$sz" -gt 5000 ] || { echo "error: $out suspiciously small ($sz bytes)" >&2; exit 1; }
  echo "wrote $out ($sz bytes)"
}

render assets-master/appicon-light.svg  "$SET/AppIcon.png"          # default = light
render assets-master/appicon-dark.svg   "$SET/AppIcon-dark.png"
render assets-master/appicon-tinted.svg "$SET/AppIcon-tinted.png"

cat > "$SET/Contents.json" <<'JSON'
{
  "images" : [
    { "filename" : "AppIcon.png", "idiom" : "universal", "platform" : "ios", "size" : "1024x1024" },
    { "appearances" : [ { "appearance" : "luminosity", "value" : "dark" } ],
      "filename" : "AppIcon-dark.png", "idiom" : "universal", "platform" : "ios", "size" : "1024x1024" },
    { "appearances" : [ { "appearance" : "luminosity", "value" : "tinted" } ],
      "filename" : "AppIcon-tinted.png", "idiom" : "universal", "platform" : "ios", "size" : "1024x1024" }
  ],
  "info" : { "author" : "xcode", "version" : 1 }
}
JSON
echo "wrote $SET/Contents.json (single-size, default+dark+tinted)"

echo "AppIcon set rendered. Gate it: scripts/actool_icon_check.sh"
