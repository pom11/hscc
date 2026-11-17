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

# ---------------------------------------------------------------------------
# The widgets that read the settings registry (t_fa7926fb: kanban monitor +
# running cards).
#
# Their views import WidgetKit and name accessory families, so nothing here can
# EXECUTE a body — the dispatch is therefore STRUCTURAL (which step every body
# takes, in which order), and the one decision in each file that is pure
# Foundation — the cadence — is sliced out of the committed source by
# GATE-SLICE marker and EXECUTED, so the Off branch is not a comment.
#
# The invariant is the same one the header of this file states: an unconfigured
# entry invites setup in every family. Paused joins that FIRST step rather than
# arriving after it, which is why the greps below look for one
# `!configured || paused` test and not for a second branch.
#
# The board-status tile above keeps the hand-written section #340 shipped with
# it. Converting that section into a call here would rewrite a merged card's
# gate for no added coverage, so this function is what the widgets AFTER it use
# — one call per surface, no copy of the checks. The day a third registry widget
# lands, it is two lines, not another forty greps.
# ---------------------------------------------------------------------------

wgfail=0
wok() { echo "  ok: $1"; }
wbad() { echo "  FAIL: $1"; wgfail=1; }

# widget_surface_gate <file> <slice-marker> <paused-factory-literal>
#                     <min gated bodies> <awk program naming the fetch fn>
#                     <registry key without the leading dot>
#                     [families mode: "accessory" (default) | "home-screen-only"]
widget_surface_gate() {
  file="$1"; slice="$2"; factory="$3"; want="$4"; region_re="$5"; key="$6"
  families="${7:-accessory}"
  [ -f "$file" ] || { wbad "missing $file"; return; }

  # (1) Every family that can draw live data gates on BOTH states in ONE step,
  # so no family can forget the second state and reach a number.
  n=$(grep -c 'if !entry.configured || entry.paused' "$file")
  if [ "$n" -ge "$want" ]; then
    wok "$n bodies gate on !configured||paused (want >= $want)"
  else
    wbad "only $n bodies gate on !configured||paused (want >= $want) — a family can draw data on a paused surface"
  fi

  # (2) The paused copy belongs to the entry, once: six families drawing six
  # different paused messages is the same drift this file's header exists for.
  # Comment lines are excluded — a header bullet that QUOTES the copy is not a
  # second owner of it, and the day one file really does own it twice the count
  # goes to 2 and this fails (that negative was run).
  n=$(grep '"Paused in Settings"' "$file" | grep -v '^[[:space:]]*//' | wc -l | tr -d ' ')
  if [ "$n" -eq 1 ]; then
    wok "one paused copy, owned by the entry (gateLine)"
  else
    wbad "\"Paused in Settings\" appears $n times (want exactly 1 — the copy must be owned by the entry)"
  fi

  # (3) The label rule, and (3b) the invite-first rule inside it. What is
  # asserted depends on whether the widget has any accessory family at all: a
  # Lock Screen ring or inline row has no room for a pane, so it must draw the
  # entry's `shortGateLabel` as TEXT (never a count — a paused ring that still
  # shows a number is the false zero §6 rule 1 refuses). A widget that ships only
  # Home Screen sizes has no such body, and a label copy nothing can reach is a
  # copy that rots, so for those the absence is what gets proven.
  if [ "$families" = "home-screen-only" ]; then
    if grep -q 'supportedFamilies(\[[^]]*\.accessory' "$file"; then
      wbad "$file names an accessory family in supportedFamilies while gated as home-screen-only — its entry now NEEDS the shortGateLabel rule this mode skips"
    else
      wok "supportedFamilies are Home Screen sizes only — no complication body that could need a label"
    fi
    # A comment is not a body: the doc comment on an entry's `gateLine` explains
    # WHY there is no label rule, and that prose must not count as a site (the
    # same rule check (2) learned the hard way on #341).
    if grep 'shortGateLabel' "$file" | grep -v '^[[:space:]]*//' | grep -q .; then
      wbad "$file carries a shortGateLabel no body can reach (no accessory family in supportedFamilies) — unreachable copy rots, delete it or add the family and switch this call to accessory mode"
    else
      wok "no shortGateLabel in a widget with no accessory family (the absence is proven, not assumed)"
    fi
  else
    # Two shapes are legitimate — the body draws `Text(entry.shortGateLabel)`
    # directly (kanban monitor), or the body draws the entry's own inline line and
    # THAT routes both gate states through `return shortGateLabel` (running cards,
    # mirroring `BoardStatusEntry.inlineLine`). Either way the string the
    # complication renders belongs to the entry.
    # Comment lines are excluded for the same reason check (2) excludes them:
    # prose about a label rule is not a body that draws one.
    n=$(grep -E 'Text\(entry\.shortGateLabel\)|return shortGateLabel' "$file" \
          | grep -v '^[[:space:]]*//' | wc -l | tr -d ' ')
    if [ "$n" -ge 2 ]; then
      wok "accessory families draw shortGateLabel ($n sites)"
    else
      wbad "only $n accessory sites route through shortGateLabel (want >= 2 — inline and circular)"
    fi

    # (3b) And the copy rule itself must put the setup invite FIRST: an
    # unconfigured device reading as "paused" is the t_5c554c5b failure wearing
    # different words. Order measured inside `var shortGateLabel`, the only place
    # the rule lives.
    copy=$(awk '/var shortGateLabel: String \{/ {f=1} f {print} f && /^    \}$/ {exit}' "$file")
    if [ -z "$copy" ]; then
      wbad "could not locate var shortGateLabel in $file — the invite-first rule is not being measured"
    else
      u=$(printf '%s\n' "$copy" | grep -n '!configured' | head -1 | cut -d: -f1)
      p=$(printf '%s\n' "$copy" | grep -n 'Paused' | head -1 | cut -d: -f1)
      if [ -n "$u" ] && [ -n "$p" ] && [ "$u" -lt "$p" ]; then
        wok "shortGateLabel checks !configured (line $u) before any paused copy (line $p) — an unconfigured device cannot read as paused"
      else
        wbad "shortGateLabel does NOT check !configured first (configured-check line=$u paused-copy line=$p)"
      fi
    fi
  fi

  # (4) Cadence and pause come from the shared registry, not a private default.
  if grep -qF "setting(for: .$key)" "$file"; then
    wok "cadence and pause come from WidgetSurfaceKind.$key (one registry, one write path)"
  else
    wbad "$file reads its cadence somewhere other than WidgetSurfaceKind.$key"
  fi

  # (5) A paused entry is built by the entry's own factory — no projection, no
  # counts, no failure pane.
  if grep -qF "$factory" "$file"; then
    wok "the provider returns the entry's own paused factory ($factory)"
  else
    wbad "the provider does not build its paused entry with $factory"
  fi

  # (6) Paused is decided BEFORE the client exists. Line order IS the claim
  # (a paused surface must spend zero of §2's request budget), and it is
  # measured INSIDE the function that owns both, because `makeIfConfigured()`
  # also occurs elsewhere in these files — comparing file-global line numbers
  # reads the wrong occurrence and fails for the wrong reason.
  region=$(awk "$region_re" "$file")
  if [ -z "$region" ]; then
    wbad "could not locate the fetch function in $file — the order claim is not being measured"
  else
    a=$(printf '%s\n' "$region" | grep -n 'if let surface, !surface.isActive' | head -1 | cut -d: -f1)
    c=$(printf '%s\n' "$region" | grep -n 'WidgetDataClient.makeIfConfigured()' | head -1 | cut -d: -f1)
    if [ -n "$a" ] && [ -n "$c" ] && [ "$a" -lt "$c" ]; then
      wok "inside the fetch: paused decided (line $a) before the client exists (line $c) — zero requests when paused"
    else
      wbad "inside the fetch, paused is NOT decided before the client is built (active=$a client=$c)"
    fi
  fi

  # (7) The cadence, EXECUTED. The marker match is END-ANCHORED, so a marker
  # that grew a suffix cannot keep this table green while the name it claims to
  # cover is gone (that negative was run: suffixing the marker fails it by name).
  s=$(grep -n "// GATE-SLICE-BEGIN: ${slice}[[:space:]]*$" "$file" | head -1 | cut -d: -f1)
  e=$(grep -n "// GATE-SLICE-END: ${slice}[[:space:]]*$" "$file" | head -1 | cut -d: -f1)
  if [ -z "$s" ] || [ -z "$e" ]; then
    wbad "GATE-SLICE marker $slice missing from $file — the cadence table is not covering the committed rule"
    return
  fi
  body=$(sed -n "$((s+1)),$((e-1))p" "$file")
  case "$body" in
    *"func nextRefreshDate"*) : ;;
    *) wbad "$slice slice does not contain nextRefreshDate" ;;
  esac
  CW="$TMPDIR/widget_cadence_${slice}_$$"
  mkdir -p "$CW"
  {
    echo "import Foundation"
    echo "enum CadenceHost {"
    echo "$body"
    echo "}"
    cat <<'SWIFT'
var fails = 0
func check(_ name: String, _ cond: Bool) {
    if cond { print("  ok: cadence " + name) } else { print("FAIL: cadence " + name); fails += 1 }
}
let t = Date(timeIntervalSince1970: 1_700_000_000)
// Off (0) is nil, and the provider turns nil into `policy: .atEnd`.
check("Off cadence yields no next date (policy .atEnd)",
      CadenceHost.nextRefreshDate(refreshMinutes: 0, from: t) == nil)
// A corrupt negative must not schedule an instant loop either.
check("negative cadence yields no next date (no refresh loop)",
      CadenceHost.nextRefreshDate(refreshMinutes: -5, from: t) == nil)
// Every real cadence is minutes, not seconds, from `now`.
check("5 min cadence is exactly +300s",
      CadenceHost.nextRefreshDate(refreshMinutes: 5, from: t) == t.addingTimeInterval(300))
check("60 min cadence is exactly +3600s",
      CadenceHost.nextRefreshDate(refreshMinutes: 60, from: t) == t.addingTimeInterval(3600))
if fails > 0 { print("RESULT: FAIL - cadence table broke"); exit(1) }
print("RESULT: OK - cadence: Off and negative mean atEnd, real values mean minutes")
SWIFT
  } > "$CW/main.swift"
  xcrun swiftc -sdk "$SDK" -o "$CW/bin" "$CW/main.swift" 2>&1 \
    || wbad "$slice cadence slice did not compile"
  if [ -x "$CW/bin" ]; then
    "$CW/bin" || wbad "$slice cadence assertions failed"
  fi
  rm -rf "$CW"
}

widget_surface_gate \
  Sources/HSCCWidgets/KanbanMonitorWidget.swift \
  kanban_monitor_cadence \
  ".paused(refreshMinutes:" \
  6 \
  '/private func fetchEntry\(surface:/ {f=1} f {print} f && /^    \}$/ {exit}' \
  widgetKanbanMonitor

# The running-cards widget: same four claims, and SIX families of its own — the
# card body's "check every family body takes the gate step, one per body" is
# what the `want` count below is for (5 here: circular, rectangular, small,
# medium, large; the inline family routes through `inlineLine`, which check (3)
# measures on the entry).
widget_surface_gate \
  Sources/HSCCWidgets/CardsRunningWidget.swift \
  cards_running_cadence \
  "return .paused()" \
  5 \
  '/private func entry\(surface:/ {f=1} f {print} f && /^    \}$/ {exit}' \
  widgetCardsRunning

# The projects widget (t_5f246177) — the last widget in this directory that was
# outside the registry, and the first one with NO accessory family (its
# `supportedFamilies` are small/medium/large), hence the `home-screen-only` mode:
# its entry deliberately has no `shortGateLabel`, and check (3-alt) is what makes
# that a proven absence instead of an oversight that quietly stops being true the
# day someone adds `.accessoryCorner`. Three gated bodies = its three families.
widget_surface_gate \
  Sources/HSCCWidgets/ProjectsWidget.swift \
  projects_cadence \
  ".paused(selectedName:" \
  3 \
  '/private func fetch\(for configuration:/ {f=1} f {print} f && /^    \}$/ {exit}' \
  widgetProjects \
  home-screen-only

if [ "$wgfail" -ne 0 ]; then
  echo "WIDGET SURFACE GATE CHECK FAILED — a registry-driven surface can draw data it was told not to fetch"
  exit 1
fi
echo "WIDGET SURFACE GATE CHECK PASSED — every registry-driven widget body gates paused and unconfigured in one step, decides pause before the client exists, and its cadence is executed off the committed source"

exit $rc
