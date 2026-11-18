#!/bin/bash
# cards_running_widget_check.sh — execute the cards surface's value rules
# against committed live fixtures. No network, no simulator.
#
# `CardsRunningWidget.swift` imports WidgetKit, so build_check.sh only ever
# COMPILES it; nothing in scripts/ could run a family body. The numbers those
# bodies draw therefore live in Foundation-only code:
#
#   * Sources/Shared/CardsRunningProjection.swift — the row projection, the
#     running/blocked counts and `CardsRunningCopy` (the numeric vocabulary);
#   * two GATE-SLICE blocks in Sources/Shared/CardsRunningActivity.swift — the
#     `RunningCardMetric(row:)` mapping and the Live Activity reading rules.
#
# This gate compiles those REAL sources (nothing is transcribed) and drives them
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
# macOS small/medium/large/XL, Live Activity lock screen + compact/expanded/
# minimal) draws those same values; and a blind or stale read never becomes a
# zero or a fresh-looking cache.
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

WIDGET=Sources/HSCCWidgets/CardsRunningWidget.swift
ACTIVITY=Sources/Shared/CardsRunningActivity.swift
PROJECTION=Sources/Shared/CardsRunningProjection.swift
LIVE_ACTIVITY=Sources/HSCCLiveActivityMonitor/CardsRunningLiveActivity.swift
DRIVER=Sources/HSCC/CardsRunningActivityDriver.swift
HARNESS=scripts/cards_running_widget_check/main.swift
FIXTURES=scripts/model_decode_check/fixtures
LIVE_FIXTURE=$FIXTURES/v1_kanban_running_cards.json
CONTRACT_FIXTURE=$FIXTURES/v1_kanban_running_contract.json

for f in "$WIDGET" "$ACTIVITY" "$PROJECTION" "$LIVE_ACTIVITY" "$DRIVER" "$HARNESS"; do
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

# The Live Activity: all four surfaces + all three Dynamic Island regions.
for token in 'ActivityConfiguration(for: CardsRunningActivityAttributes.self)' \
             'dynamicIsland' 'compactLeading: {' 'compactTrailing: {' 'minimal: {' \
             'DynamicIslandExpandedRegion(.leading)' \
             'DynamicIslandExpandedRegion(.center)' \
             'DynamicIslandExpandedRegion(.trailing)' \
             'DynamicIslandExpandedRegion(.bottom)'; do
  expect "Live Activity has $token" "1" "$(grep -cF "$token" "$LIVE_ACTIVITY")"
done

# The one row mapping: the driver must use the sliced initializer, not a
# hand-written field copy that the gate cannot see.
expect "the driver maps rows through RunningCardMetric(row:)" "1" \
  "$(grep -c 'RunningCardMetric.init(row:)' "$DRIVER")"
report "the driver does not rebuild a metric field by field" \
  "$(grep -n 'RunningCardMetric(board:' "$DRIVER")"
expect "the row-metric block declares the mapping" "1" \
  "$(grep -c 'init(row: CardsRunningRow)' "$ACTIVITY")"

# Freshness: both surfaces must draw a self-updating relative age, and neither
# may keep the old frozen "read just now" copy.
expect "the widget footer draws a relative updated age" "1" \
  "$(grep -c 'Text(entry.updatedAt, style: .relative)' "$WIDGET")"
expect "the Lock Screen draws a relative updated age" "1" \
  "$(grep -c 'Text(state.updatedAt, style: .relative)' "$LIVE_ACTIVITY")"
report "no surface hard-codes a fresh 'read just now' claim" \
  "$(grep -n 'read just now' "$WIDGET" "$LIVE_ACTIVITY")"

# §8.3 / WIDGET_ROUTES: the card list comes from /v1/kanban/running + blocked;
# /v1/cards is not an extension route.
report "the cards surface never names /v1/cards in code" \
  "$(grep -n '/v1/cards' "$WIDGET" "$ACTIVITY" "$PROJECTION" "$DRIVER" \
      | grep -vE '^[^:]+:[0-9]+:[[:space:]]*//' || true)"
expect "the projection reads kanban/running" "1" \
  "$(grep -c 'client.kanbanRunning()' "$PROJECTION")"

# The fixtures are scrubbed: no private address, home path or token path.
report "fixtures carry no private address/path/token" \
  "$(grep -nE '100\.(6[4-9]|[7-9][0-9]|1[0-2][0-9])\.|192\.168\.|10\.[0-9]+\.[0-9]+\.|/Users/|/home/[a-z]|\.hscc|Bearer ' "$LIVE_FIXTURE" "$CONTRACT_FIXTURE" || true)"

# project.yml is the only place the gates see a file. The projection is now
# compiled into the Live Activity extension because the sliced metric names it.
ext_block=$(python3 - <<'PY'
import re
spec = open('project.yml').read().split('\ntargets:\n', 1)[1]
blk = spec.split('\n  HSCCLiveActivityMonitor:', 1)[1]
blk = re.split(r'\n  [A-Za-z]\w*:\n', blk, maxsplit=1)[0]
print(blk.count('Sources/Shared/CardsRunningActivity.swift')
      + blk.count('Sources/Shared/CardsRunningProjection.swift'))
PY
)
expect "HSCCLiveActivityMonitor compiles the activity + row projection" "2" "$ext_block"

# Anti-mirror: the harness declares none of the subject's types.
report "the harness re-declares no subject type" \
  "$(grep -nE '^(struct|enum|class|extension|typealias) +(CardsRunning|RunningCard|KanbanRunning|WidgetData|SharedStore)' "$HARNESS")"

echo ""
echo "executing the real projection + sliced activity rules against the fixtures..."

slice() {  # slice <file> <marker> — print the bracketed block, markers excluded
  awk -v b="// GATE-SLICE-BEGIN: $2" -v e="// GATE-SLICE-END: $2" \
      '$0==b{s=1;next} $0==e{s=0} s' "$1"
}

printf 'import Foundation\n' > "$TMP/SlicedRules.swift"
for name in cards-row-metric cards-reading-rules; do
  grep -q "^// GATE-SLICE-BEGIN: ${name}$" "$ACTIVITY" || {
    echo "FAIL: $ACTIVITY has no 'GATE-SLICE-BEGIN: ${name}' marker." >&2
    echo "      The rules moved — re-point this gate at them." >&2
    exit 1
  }
  slice "$ACTIVITY" "$name" > "$TMP/one.swift"
  [ -s "$TMP/one.swift" ] || { echo "FAIL: empty slice for '$name'" >&2; exit 1; }
  cat "$TMP/one.swift" >> "$TMP/SlicedRules.swift"
done

if ! xcrun --sdk "$SDK" swiftc -o "$TMP/check" \
     Sources/Shared/SharedModels.swift \
     Sources/Shared/SharedStore.swift \
     Sources/Shared/WidgetDataClient.swift \
     Sources/Shared/CardsRunningProjection.swift \
     "$TMP/SlicedRules.swift" \
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
echo "CARDS RUNNING WIDGET CHECK PASSED — every family, both platforms and the Live Activity draw the live fixture's own fields"
exit 0
