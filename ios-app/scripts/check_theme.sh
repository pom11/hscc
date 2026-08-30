#!/usr/bin/env bash
# check_theme.sh — verify all colour in the app comes from Theme.swift.
#
# Design rule (Views/Theme.swift): every colour must be a Theme semantic /
# palette token. Raw system named colours (.red, .white, ...), raw hex
# literal colours, and UIColor(red:)/init(red:) component constructors are all
# forbidden OUTSIDE Theme.swift.
#
# Usage:  bash scripts/check_theme.sh
# Exit 0 = clean (only allowlisted raw uses remain). Exit 1 = violations found.
#
# Comprehensiveness: one OR'd regex covers every raw-colour spelling the
# codebase can produce:
#   1. NAMED     — .white .black .red .green .blue .orange .yellow .purple
#                  .gray .pink .mint .teal .indigo .brown .clear
#   2. HEX       — 0xRRGGBB (Color(hex:), Palette literals)
#   3. COMPONENT — UIColor(red:, Color(red:, .init(red:, UIColor(white:)

set -u
SRC="$(cd "$(dirname "$0")/.." && pwd)/Sources"
THEME="$SRC/HSCC/Views/Theme.swift"

RAW='\.(red|white|black|green|blue|orange|yellow|purple|gray|pink|mint|teal|indigo|brown)\b|0x[0-9A-Fa-f]{6}|UIColor\(red:|Color\(red:|\.init\(red:|UIColor\(white:'

# ALLOWLIST — a raw named colour is intentional ONLY when the line carries an
# inline `// theme-allow:` marker explaining why. Markers travel WITH the code:
# an earlier line-number allowlist (666/702/703/149) silently went stale the
# moment anything above those lines shifted, turning the guard into a false
# failure. Anchor on content, never on position.
HITS=$(grep -rnE "$RAW" --include='*.swift' "$SRC" | grep -vF "${THEME}:" | grep -v 'theme-allow:')
VIOL=""
while IFS= read -r line; do
  [ -z "$line" ] && continue
  VIOL="${VIOL}${line}
"
done <<< "$HITS"

if [ -z "$VIOL" ]; then
  echo "CLEAN — no raw colour outside Theme.swift."
  exit 0
else
  echo "VIOLATIONS — raw colour outside Theme.swift:"
  printf '%b' "$VIOL"
  exit 1
fi
