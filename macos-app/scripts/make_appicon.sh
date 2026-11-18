#!/usr/bin/env bash
# make_appicon.sh — render the macOS AppIcon set from the iOS master SVGs.
#
# The icon design is owned in ONE place: ios-app/assets-master/appicon-*.svg
# (solid black field + flat #FF5722 block H, operator-selected 2026-11-18).
# This script only rasterises that master into the macOS sizes — it does not
# redraw, crop or invent an icon, and hand-dropped PNGs are never committed.
# The menu bar mark is separate: Assets.xcassets/MenuBarIcon.imageset carries
# the H alone as a template SVG (monochrome, system-tinted).
#
# macOS still asks for the classic 16/32/128/256/512 1x + 2x ladder. Every PNG
# is rendered from the vector at its target size, so small sizes stay crisp
# instead of being downsampled once more.
#
# Requires: rsvg-convert (brew install librsvg), sips.
# Usage:    macos-app/scripts/make_appicon.sh
# Exit:     0 wrote every PNG + Contents.json, 1 missing tool/master/bad output.
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

command -v rsvg-convert >/dev/null || { echo "error: rsvg-convert not found (brew install librsvg)" >&2; exit 1; }
command -v sips >/dev/null || { echo "error: sips not found (macOS command-line tools)" >&2; exit 1; }

MASTER="../ios-app/assets-master/appicon-dark.svg"
[ -f "$MASTER" ] || { echo "error: missing icon master $MASTER" >&2; exit 1; }

SET="Sources/HSCCMac/Assets.xcassets/AppIcon.appiconset"
rm -rf "$SET"
mkdir -p "$SET"

render() {  # render <pixels> <out-png>
  local size="$1" out="$2"
  rsvg-convert -w "$size" -h "$size" "$MASTER" -o "$out"
  local dim; dim=$(sips -g pixelWidth -g pixelHeight "$out" | awk '/pixelWidth/{w=$2}/pixelHeight/{h=$2}END{print w"x"h}')
  [ "$dim" = "${size}x${size}" ] || { echo "error: $out is $dim, expected ${size}x${size}" >&2; exit 1; }
  local bytes; bytes=$(stat -f%z "$out")
  # The 16 px render of the flat block H is ~220 B; a blank square compresses
  # below 100 B. Keep the floor low enough for the smallest legal size while
  # still rejecting an empty render.
  [ "$bytes" -gt 100 ] || { echo "error: $out suspiciously small ($bytes bytes)" >&2; exit 1; }
  echo "wrote $out ($size px, $bytes bytes)"
}

render 16   "$SET/icon_16x16.png"
render 32   "$SET/icon_16x16@2x.png"
render 32   "$SET/icon_32x32.png"
render 64   "$SET/icon_32x32@2x.png"
render 128  "$SET/icon_128x128.png"
render 256  "$SET/icon_128x128@2x.png"
render 256  "$SET/icon_256x256.png"
render 512  "$SET/icon_256x256@2x.png"
render 512  "$SET/icon_512x512.png"
render 1024 "$SET/icon_512x512@2x.png"

cat > "$SET/Contents.json" <<'JSON'
{
  "images" : [
    { "filename" : "icon_16x16.png",      "idiom" : "mac", "scale" : "1x", "size" : "16x16" },
    { "filename" : "icon_16x16@2x.png",   "idiom" : "mac", "scale" : "2x", "size" : "16x16" },
    { "filename" : "icon_32x32.png",      "idiom" : "mac", "scale" : "1x", "size" : "32x32" },
    { "filename" : "icon_32x32@2x.png",   "idiom" : "mac", "scale" : "2x", "size" : "32x32" },
    { "filename" : "icon_128x128.png",    "idiom" : "mac", "scale" : "1x", "size" : "128x128" },
    { "filename" : "icon_128x128@2x.png", "idiom" : "mac", "scale" : "2x", "size" : "128x128" },
    { "filename" : "icon_256x256.png",    "idiom" : "mac", "scale" : "1x", "size" : "256x256" },
    { "filename" : "icon_256x256@2x.png", "idiom" : "mac", "scale" : "2x", "size" : "256x256" },
    { "filename" : "icon_512x512.png",    "idiom" : "mac", "scale" : "1x", "size" : "512x512" },
    { "filename" : "icon_512x512@2x.png", "idiom" : "mac", "scale" : "2x", "size" : "512x512" }
  ],
  "info" : { "author" : "xcode", "version" : 1 }
}
JSON

echo "wrote $SET/Contents.json (classic macOS 10-size ladder)"
echo "AppIcon set rendered from $MASTER — never hand-drop these PNGs."
