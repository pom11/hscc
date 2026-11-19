#!/bin/bash
# kanban_monitor_widget_check.sh — execute the Kanban Monitor widget's board
# table against a scrubbed live capture of the compact summary route.
#
# Why this exists: the widget draws five numbers per board (Running, Ready,
# Todo, Blocked, Done 24h). A SwiftUI body cannot render headlessly, so the
# projection, sort, refusal rules and the text the families draw live in
# `KanbanMonitorWidget.swift` as plain values the macOS harness can execute.
# The gate asserts those values ARE the fixture's fields — not a plausible
# transcription of them.
#
# No network, no simulator, no device. Usage: ios-app/scripts/kanban_monitor_widget_check.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
SDK=$(xcrun --sdk macosx --show-sdk-path 2>/dev/null) || {
  echo "error: no macOS SDK (is Xcode installed?)" >&2; exit 1; }

. scripts/swift_source_groups.sh

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

SUBJECT=Sources/HSCCWidgets/KanbanMonitorWidget.swift
HARNESS=scripts/kanban_monitor_widget_check/main.swift
FIXTURES=scripts/kanban_monitor_widget_check/fixtures
SHARED=scripts/model_decode_check/fixtures

for f in "$SUBJECT" "$HARNESS" "$FIXTURES/v1_mobile_summary_live_2026-11-18.json" \
         "$SHARED/v1_mobile_summary_degraded.json" \
         "$SHARED/v1_mobile_summary_truncated.json" \
         "$SHARED/v1_mobile_summary_board_error.json"; do
  [ -f "$f" ] || { echo "error: $f missing" >&2; exit 1; }
done

rc=0
report() {
  if [ -n "${2:-}" ]; then echo "FAIL: $1"; echo "$2" | sed 's/^/    /'; rc=1
  else echo "  ok: $1"; fi
}
count_is() {
  local got
  got=$(grep -cE "$3" "$2" || true)
  if [ "$got" = "$4" ]; then echo "  ok: $1 ($got)"
  else echo "FAIL: $1 — got $got, want $4"; rc=1; fi
}

echo "structural proofs on $SUBJECT"
report "no /v1/ route literal in the widget" "$(grep -nE '"(/v1[^"]*)"' "$SUBJECT")"
report "no legacy nil-returning get() call" "$(grep -nE '\.get\(' "$SUBJECT")"
count_is "the board table has exactly one read" "$SUBJECT" 'client\.mobileSummary\(' 1
count_is "it builds the client the one sanctioned way" "$SUBJECT" \
  'WidgetDataClient\.makeIfConfigured\(\)' 1
count_is "all six family bodies gate paused+unconfigured in one step" "$SUBJECT" \
  'if !entry\.configured \|\| entry\.paused' 6
count_is "the paused copy has one owner" "$SUBJECT" '"Paused in Settings"' 1
count_is "the paused factory is built by the provider" "$SUBJECT" \
  'return \.paused\(refreshMinutes:' 1
count_is "the provider reads its registry entry" "$SUBJECT" \
  'setting\(for: \.widgetKanbanMonitor\)' 1
count_is "the cadence slice markers are the pair the gate executes" "$SUBJECT" \
  '// GATE-SLICE-(BEGIN|END): kanban_monitor_cadence$' 2
count_is "§7 touches the App Group suite twice, in the store only" "$SUBJECT" \
  'SharedStore\.suite' 2

echo ""
echo "compiling the real widget + shared sources + harness into a macOS CLI..."
real_sources=(
  "$SUBJECT"
  Sources/HSCCWidgets/WidgetRendering.swift
  "${SWIFT_GROUP_WIDGET_CLIENT[@]}"
)
harness_sources=(
  scripts/projects_widget_check/ThemeStub.swift
  "$HARNESS"
)
if ! xcrun --sdk "$SDK" swiftc -o "$TMP/kanban_monitor_widget_check" \
     "${real_sources[@]}" "${harness_sources[@]}" 2>"$TMP/compile.err"; then
  echo "error: failed to compile the real widget sources — see below" >&2
  cat "$TMP/compile.err" >&2
  exit 1
fi
if [ -s "$TMP/compile.err" ]; then
  echo "compiler diagnostics (treated as findings):" >&2
  cat "$TMP/compile.err" >&2
  rc=1
fi

echo "running the board table against the live capture... (no network)"
"$TMP/kanban_monitor_widget_check" "$PWD/$FIXTURES" "$PWD/$SHARED" || rc=1

echo ""
if [ "$rc" -eq 0 ]; then
  echo "KANBAN MONITOR WIDGET CHECK PASSED — every drawn number equals its live-capture field, done is Done 24h, and degraded/truncated payloads are refused"
else
  echo "KANBAN MONITOR WIDGET CHECK FAILED — see above"
fi
exit "$rc"
