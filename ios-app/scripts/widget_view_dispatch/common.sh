#!/bin/bash
# common.sh — the shared plumbing for the per-surface widget dispatch checks.
#
# SOURCED, never run: by scripts/widget_view_dispatch_check.sh (the thin
# aggregator) and by every <surface>.sh in this directory. Nothing in this file
# asserts anything about a widget. It owns the cd to ios-app, the macOS SDK, the
# bok/wok/bfail/wbad verdict helpers, and widget_surface_gate — the one check
# procedure the registry-driven widgets share. A surface file must never
# re-declare any of it; that duplication is what made the pre-split harness
# collide on every pair of concurrent widget cards.
#
# Split out of widget_view_dispatch_check.sh by t_2c22e60d. The checks moved
# verbatim, so their verdict text is the contract — see references/check-inventory.md.
#
# Sourced under `set -uo pipefail` by every caller.

# Resolve this file's own directory BEFORE the cd below. The documented
# invocation is ./ios-app/scripts/widget_view_dispatch_check.sh from the repo
# root, and a script that locates itself only after a cd reads the path the
# caller typed instead of its own.
WVD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# TWO levels up: this file lives in ios-app/scripts/widget_view_dispatch/, one
# directory deeper than the aggregator that sources it. The pre-split script
# needed a single ".." from ios-app/scripts/; copying that here lands in
# scripts/ and every relative Sources/ path then reads as missing.
IOS_APP_DIR="$(cd "$WVD_DIR/../.." && pwd)"
cd "$IOS_APP_DIR" || exit 1

# Every check below measures committed Swift by relative path into ios-app, so
# they all depend on that cd.

# wvd_sdk — echo the macOS SDK path, refusing exactly the way the pre-split
# script did (message on stderr, then exit), and cache it in WVD_SDK so a run
# over several surfaces asks xcrun once. The aggregator resolves and exports it
# up front, which keeps "no macOS SDK" a single message per run.
wvd_sdk() {
  if [ -z "${WVD_SDK:-}" ]; then
    WVD_SDK=$(xcrun --sdk macosx --show-sdk-path 2>/dev/null) || { echo "no macOS SDK" >&2; return 1; }
  fi
  printf '%s\n' "$WVD_SDK"
}

# The verdict helpers. Both names of each pair are kept on purpose: every moved
# check body still calls the helper it was written against, so the bodies are
# byte-identical to the pre-split file. Pre-split bfail bumped a script-wide rc
# and wbad bumped wgfail; both now bump one per-file counter, because a surface
# file's exit status is the only thing its aggregator can see.
FAILURES=0
bok()   { echo "  ok: $1"; }
wok()   { echo "  ok: $1"; }
bfail() { echo "  FAIL: $1"; FAILURES=1; }
wbad()  { echo "  FAIL: $1"; FAILURES=1; }

# wvd_finish — the last line of every surface file. A FAIL printed anywhere in
# that file must reach the aggregator as a non-zero exit; without this a check
# could fail, print nothing but ok lines, and the run would go green.
wvd_finish() { [ "$FAILURES" -eq 0 ]; }

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
