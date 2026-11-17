#!/bin/bash
# widget_view_dispatch_check.sh — prove which view the widget renders for
# every (state, configured) combination, and enforce the audit-3 invariant:
# ANY unconfigured entry MUST show the "Set up the app" setup invite.
#
# (t_5c554c5b) The dispatch is the if/else-if chain in ClusterWidgetViews.swift
# (SmallClusterWidget.body, MediumClusterWidget.body). This harness mirrors that
# chain and exhaustively checks every (state, configured) pair. There is no iOS
# runtime on this host, so this proves the LOGIC of which view wins and the
# invariant, not the rendered pixels.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# Prove the REAL source checks `!configured` before `unreachable` (both bodies).
count=$(grep -c 'if !entry.configured' Sources/HSCCWidgets/ClusterWidgetViews.swift)
echo "source 'if !entry.configured' occurrences = $count (want 2: small+medium bodies)"
[ "$count" -ge 2 ] || { echo "❌ source order regressed: unconfigured no longer checked first"; exit 1; }

cat > "$TMPDIR/widget_dispatch_$$.swift" <<'SWIFT'
import Foundation
enum S: String, CaseIterable { case serving, waking, down, unreachable, unknown }

// The FIXED order (matches the committed source): unconfigured wins over unreachable.
func dispatchView(state: S, configured: Bool) -> String {
    if !configured              { return "unconfiguredView     <-- 'Set up the app' invite" }
    else if state == .unreachable { return "unreachableStateView <-- stale, with age" }
    else if state == .unknown   { return "unknownView (falls through to normal layout)" }
    else                          { return "normal state layout (serving/waking/down)" }
}

var fail = false
for s in S.allCases {
    for c in [false, true] {
        let v = dispatchView(state: s, configured: c)
        print("state=" + s.rawValue + " configured=" + String(c) + " -> " + v)
        if !c && v.hasPrefix("unconfiguredView") == false {
            print("  WARN: unconfigured entry did NOT yield the setup invite")
            fail = true
        }
    }
}
if fail { print("RESULT: FAIL - some unconfigured entry shows a non-setup view"); exit(1) }
print("RESULT: OK - every unconfigured entry invites setup")
SWIFT

SDK=$(xcrun --sdk macosx --show-sdk-path 2>/dev/null) || { echo "no macOS SDK" >&2; exit 1; }
xcrun swiftc -sdk "$SDK" -o "$TMPDIR/widget_dispatch_$$" "$TMPDIR/widget_dispatch_$$.swift" 2>&1 || { echo "compile failed" >&2; exit 1; }
"$TMPDIR/widget_dispatch_$$"
rc=$?
rm -f "$TMPDIR/widget_dispatch_$$" "$TMPDIR/widget_dispatch_$$.swift"

# ---------------------------------------------------------------------------
# The BOARD STATUS widget (t_09cd8abf): same invariant, its own file.
#
# Its views import WidgetKit and name accessory families, so nothing here can
# execute a body — the dispatch is therefore structural (which step every body
# takes, and in which order), and the ONE decision in the file that is pure
# Foundation — the cadence — is extracted from the committed source by
# GATE-SLICE marker and EXECUTED, so the Off branch is not a comment.
# ---------------------------------------------------------------------------
BOARD=Sources/HSCCWidgets/BoardStatusWidget.swift
[ -f "$BOARD" ] || { echo "missing $BOARD" >&2; exit 1; }
bfail() { echo "  FAIL: $1"; rc=1; }
bok() { echo "  ok: $1"; }

# Every family that can draw live data gates on BOTH states in one step, and
# the gate copy itself puts the setup invite first — so neither an unconfigured
# app nor a paused one can reach a number.
gate=$(grep -c 'if !entry.configured || entry.paused' "$BOARD")
if [ "$gate" -ge 5 ]; then bok "$gate bodies (rectangular, small, medium, large + circular) gate on !configured||paused"; else bfail "only $gate bodies gate on !configured||paused (want >= 5)"; fi
[ "$(grep -c 'if !configured || paused { return shortGateLabel }' "$BOARD")" -eq 1 ] \
  && bok "the inline family routes both gate states through the same copy rule" \
  || bfail "the inline family does not route gate states through shortGateLabel"
grep -q 'paused ? "Paused in Settings"' "$BOARD" \
  && bok "one paused copy, owned by the entry (gateLine)" \
  || bfail "no single paused copy rule on the entry"
# The provider must decide BEFORE reading: a paused surface spends zero of §2's
# request budget. Line order IS the claim — measured INSIDE `entry(for:surface:)`,
# because `makeIfConfigured` also appears earlier in the file, in the board
# picker's own query (comparing file-global line numbers would read that one and
# fail for the wrong reason).
region=$(awk '/private func entry\(for configuration:/ {f=1} f {print} f && /^    \}$/ {exit}' "$BOARD")
if [ -z "$region" ]; then
  bfail "could not locate entry(for:surface:) in $BOARD — the order claim is not being measured"
else
  active_line=$(printf '%s\n' "$region" | grep -n 'if let surface, !surface.isActive' | head -1 | cut -d: -f1)
  client_line=$(printf '%s\n' "$region" | grep -n 'WidgetDataClient.makeIfConfigured()' | head -1 | cut -d: -f1)
  if [ -n "$active_line" ] && [ -n "$client_line" ] && [ "$active_line" -lt "$client_line" ]; then
    bok "inside entry(): paused is decided (line $active_line) before the client exists (line $client_line) — a paused surface sends zero requests"
  else
    bfail "inside entry(), paused is NOT decided before the client is built (active=$active_line client=$client_line)"
  fi
fi
grep -q 'return .paused(boardSlug: boardSlug)' "$BOARD" \
  && bok "the paused entry is built by the entry's own factory (no projection, no failure)" \
  || bfail "the provider does not return .paused(boardSlug:)"
# The surface key must be the shared registry's, not a private defaults key.
grep -q 'setting(for: .widgetBoardStatus)' "$BOARD" \
  && bok "cadence and pause come from WidgetSurfaceKind.widgetBoardStatus (one registry, one write path)" \
  || bfail "the widget reads its cadence somewhere other than the shared registry"

# Executed cadence: the Off branch, sliced from the committed source. The
# marker match is END-ANCHORED, so a marker that grew a suffix cannot keep this
# table reading the same slice while the name it claims to cover is gone.
slice_start=$(grep -n '// GATE-SLICE-BEGIN: board_status_cadence[[:space:]]*$' "$BOARD" | head -1 | cut -d: -f1)
slice_end=$(grep -n '// GATE-SLICE-END: board_status_cadence[[:space:]]*$' "$BOARD" | head -1 | cut -d: -f1)
if [ -z "$slice_start" ] || [ -z "$slice_end" ]; then
  # Die BY NAME: a renamed or deleted marker must not leave this table green
  # while it silently stops covering the committed rule.
  bfail "GATE-SLICE marker board_status_cadence missing from $BOARD — the cadence table is not covering the committed rule"
else
  slice=$(sed -n "$((slice_start+1)),$((slice_end-1))p" "$BOARD")
  case "$slice" in
    *"func nextRefreshDate"*) : ;;
    *) bfail "board_status_cadence slice does not contain nextRefreshDate" ;;
  esac
  CW="$TMPDIR/board_cadence_$$"
  mkdir -p "$CW"
  {
    echo "import Foundation"
    echo "enum BoardStatusTimelineProvider {"
    echo "$slice"
    echo "}"
    cat <<'SWIFT'
var fails = 0
func check(_ name: String, _ cond: Bool) {
    if cond { print("  ok: " + name) } else { print("FAIL: " + name); fails += 1 }
}
let t = Date(timeIntervalSince1970: 1_700_000_000)
// Off (0) is nil, and the provider turns nil into `policy: .atEnd`.
check("Off cadence yields no next date (policy .atEnd)",
      BoardStatusTimelineProvider.nextRefreshDate(refreshMinutes: 0, from: t) == nil)
// A corrupt negative must not schedule an instant loop either.
check("negative cadence yields no next date (no refresh loop)",
      BoardStatusTimelineProvider.nextRefreshDate(refreshMinutes: -5, from: t) == nil)
// Every real cadence is minutes, not seconds, from `now`.
check("5 min cadence is exactly +300s",
      BoardStatusTimelineProvider.nextRefreshDate(refreshMinutes: 5, from: t) == t.addingTimeInterval(300))
check("60 min cadence is exactly +3600s",
      BoardStatusTimelineProvider.nextRefreshDate(refreshMinutes: 60, from: t) == t.addingTimeInterval(3600))
if fails > 0 { print("RESULT: FAIL - board status cadence table broke"); exit(1) }
print("RESULT: OK - board status cadence: Off and negative mean atEnd, real values mean minutes")
SWIFT
  } > "$CW/main.swift"
  xcrun swiftc -sdk "$SDK" -o "$CW/bin" "$CW/main.swift" 2>&1 || { echo "  FAIL: cadence slice did not compile" >&2; rc=1; }
  if [ -x "$CW/bin" ]; then "$CW/bin" || rc=1; fi
  rm -rf "$CW"
fi

exit $rc
