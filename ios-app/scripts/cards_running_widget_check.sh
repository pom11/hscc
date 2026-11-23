#!/bin/bash
# cards_running_widget_check.sh — execute the cards surface's value rules
# against committed live fixtures. No network, no simulator.
#
# `CardsRunningWidget.swift` imports WidgetKit, so build_check.sh only ever
# COMPILES it; nothing in scripts/ could run a family body. The numbers those
# bodies draw live in Foundation-only code:
#
#   * Sources/Shared/CardsRunningProjection.swift — the row projection, the
#     running/blocked counts and `CardsRunningCopy` (the numeric vocabulary).
#
# This gate compiles that REAL source (nothing is transcribed) and drives it
# from two fixtures:
#
#   * v1_kanban_running_cards.json — a scrubbed capture of a live
#     GET /v1/kanban/running (the older server's shape: assignee + started_at).
#   * v1_kanban_running_contract.json — the same route's fixed shape from
#     `kanban_live.CARD_VIEW_FIELDS` (worker + run_s + hb_s), so the mapping
#     that prefers the CLI's `worker` column and `run_s` is executed too.
#
# WHAT IT PROVES, by requirement: every number/row a family shows maps 1:1 to a
# fixture field; the count is the number of running cards (`hscc cards monitor
# --short`); each family (iOS small/medium/large + inline/circular/rectangular,
# macOS small/medium/large/XL) draws those same values; and a blind or stale
# read never becomes a zero or a fresh-looking cache.
#
# WHAT IT DOES NOT PROVE: no pixels are rendered, so layout, fonts and colour are
# out; WordWidgetKit's own scheduling is not driven. `widget_view_dispatch_check`
# owns the gate order, `widget_style_check` owns the style plumbing, and both
# `build_check.sh` scripts own the compile.
#
# Usage: ios-app/scripts/cards_running_widget_check.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
SDK=$(xcrun --sdk macosx --show-sdk-path 2>/dev/null) || {
  echo "error: no macOS SDK (is Xcode installed?)" >&2; exit 1; }

. scripts/swift_source_groups.sh

WIDGET=Sources/HSCCWidgets/CardsRunningWidget.swift
PROJECTION=Sources/Shared/CardsRunningProjection.swift
HARNESS=scripts/cards_running_widget_check/main.swift
FIXTURES=scripts/model_decode_check/fixtures
LIVE_FIXTURE=$FIXTURES/v1_kanban_running_cards.json
CONTRACT_FIXTURE=$FIXTURES/v1_kanban_running_contract.json

for f in "$WIDGET" "$PROJECTION" "$HARNESS"; do
  [ -f "$f" ] || { echo "error: $f missing" >&2; exit 1; }
done
for f in "$LIVE_FIXTURE" "$CONTRACT_FIXTURE" "$FIXTURES/kanban_blocked.json"; do
  [ -f "$f" ] || { echo "error: fixture $f missing" >&2; exit 1; }
done

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

rc=0
report() {  # report <label> <grep-output> — non-empty output is a failure
  if [ -n "${2:-}" ]; then
    echo "FAIL: $1"; echo "$2" | sed 's/^/    /'; rc=1
  else
    echo "  ok: $1"
  fi
}
expect() {  # expect <label> <want> <got>
  if [ "${3:-}" = "$2" ]; then echo "  ok: $1 ($3)"; else
    echo "FAIL: $1 — want $2, got ${3:-none}"; rc=1; fi
}

echo "structural proofs (the SwiftUI bodies a macOS SDK cannot execute)"

# Every declared family has a renderer. iOS: six; macOS: four (XL reuses large).
ios_families=$(grep -c 'accessoryInline, .accessoryCircular, .accessoryRectangular' "$WIDGET")
expect "iOS declares the six families" "1" "$ios_families"
expect "macOS declares Small/Medium/Large/XL" "1" \
  "$(grep -c '\.systemSmall, \.systemMedium, \.systemLarge, \.systemExtraLarge' "$WIDGET")"
for view in CardsRunningSmallView CardsRunningMediumView CardsRunningLargeView \
            CardsRunningCircularView CardsRunningRectangularView; do
  expect "$view has a body" "1" "$(grep -c "^struct $view: View" "$WIDGET")"
done
expect "inline draws the entry's inline line" "1" \
  "$(grep -c 'Text(entry.inlineLine)' "$WIDGET")"
expect "XL reuses the large renderer" "1" \
  "$(grep -c 'case .systemExtraLarge:' "$WIDGET")"

# Freshness: the Native widget draws the DATA's own clock through the shared
# AsOfStamp (the 2026-11-20 redesign replaced the render-relative age on the
# widget). It may not keep the old frozen "read just now" copy.
expect "the widget draws the data's own clock (AsOfStamp)" "1" \
  "$([ "$(grep -c 'AsOfStamp(date: entry.dataAt)' "$WIDGET")" -ge 1 ] && echo 1)"
report "the widget does not hard-code a fresh 'read just now' claim" \
  "$(grep -n 'read just now' "$WIDGET")"

# §8.3 / WIDGET_ROUTES: the card list comes from /v1/kanban/running + blocked;
# /v1/cards is not an extension route.
report "the cards surface never names /v1/cards in code" \
  "$(grep -n '/v1/cards' "$WIDGET" "$PROJECTION" \
      | grep -vE '^[^:]+:[0-9]+:[[:space:]]*//' || true)"
expect "the projection reads kanban/running" "1" \
  "$(grep -c 'client.kanbanRunning()' "$PROJECTION")"

# The fixtures are scrubbed: no private address, home path or token path.
report "fixtures carry no private address/path/token" \
  "$(grep -nE '100\.(6[4-9]|[7-9][0-9]|1[0-2][0-9])\.|192\.168\.|10\.[0-9]+\.[0-9]+\.|/Users/|/home/[a-z]|\.hscc|Bearer ' "$LIVE_FIXTURE" "$CONTRACT_FIXTURE" || true)"

# project.yml is the only place the gates see a file. The projection is owned
# by the widget target (and the desktop target by path); it must be listed
# there or this gate would run a file the app never builds.
widget_block=$(python3 - <<'PY'
import re
spec = open('project.yml').read().split('\ntargets:\n', 1)[1]
blk = spec.split('\n  HSCCWidgets:', 1)[1]
blk = re.split(r'\n  [A-Za-z]\w*:\n', blk, maxsplit=1)[0]
print(1 if '- path: Sources/HSCCWidgets' in blk else 0)
PY
)
expect "HSCCWidgets computes its source directory" "1" "$widget_block"

# Anti-mirror: the harness declares none of the subject's types.
report "the harness re-declares no subject type" \
  "$(grep -nE '^(struct|enum|class|extension|typealias) +(CardsRunning|RunningCard|KanbanRunning|WidgetData|SharedStore)' "$HARNESS")"

echo ""
echo "executing the real projection against the fixtures..."

if ! xcrun --sdk "$SDK" swiftc -o "$TMP/check" \
     "${SWIFT_GROUP_WIDGET_CLIENT[@]}" \
     Sources/Shared/CardsRunningProjection.swift \
     scripts/model_decode_check/ThemeStub.swift \
     "$HARNESS" 2>"$TMP/compile.err"; then
  echo "FAIL: the real sources + harness did not compile" >&2
  cat "$TMP/compile.err" >&2
  exit 1
fi

FIXDIR=$(cd "$FIXTURES" && pwd)
"$TMP/check" "$FIXDIR" || rc=1

if [ "$rc" -ne 0 ]; then
  echo "CARDS RUNNING WIDGET CHECK FAILED — a family can draw a value its fixture did not send"
  exit 1
fi
echo "CARDS RUNNING WIDGET CHECK PASSED — every family, both platforms draw the live fixture's own fields"
exit 0
