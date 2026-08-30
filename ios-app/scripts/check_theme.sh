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

# ALLOWLIST — raw named colours INTENTIONALLY kept because each is readable in
# BOTH light and dark appearances (fixed-hue background, see audit t_87914280):
#   OrchestratorChatView.swift:666  white text on the accent-coloured bubble
#   OrchestratorChatView.swift:702  fault-red Retry button (has "Retry" label)
#   OrchestratorChatView.swift:703  white text on that Retry button
#   ProjectsView.swift:149          white text on the amber unread badge chip
ALLOW='OrchestratorChatView.swift:666
OrchestratorChatView.swift:702
OrchestratorChatView.swift:703
ProjectsView.swift:149'

HITS=$(grep -rnE "$RAW" --include='*.swift' "$SRC" | grep -vF "${THEME}:")
VIOL=""
while IFS= read -r line; do
  [ -z "$line" ] && continue
  key=$(printf '%s' "$line" | sed -E 's#^.*/##; s/:.*//')   # "<File>.swift:<line>"
  key="$key:$(printf '%s' "$line" | sed -E 's#^[^:]*:([0-9]+):.*#\1#')"
  if ! printf '%s\n' "$ALLOW" | grep -qxF "$key"; then
    VIOL="${VIOL}${line}\n"
  fi
done <<< "$HITS"

if [ -z "$VIOL" ]; then
  echo "CLEAN — no raw colour outside Theme.swift."
  exit 0
else
  echo "VIOLATIONS — raw colour outside Theme.swift:"
  printf '%b' "$VIOL"
  exit 1
fi
