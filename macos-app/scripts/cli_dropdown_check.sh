#!/bin/bash
# cli_dropdown_check.sh — the macOS menu bar dropdown draws the CLI's text.
#
# Two halves:
#
#   1. EXECUTED. Compiles the SHIPPED Foundation-only dropdown layer
#      (macos-app/Sources/HSCCMac/DashboardFleetReading.swift) together with the
#      real SharedModels / SharedStore / ClusterMonitorViewModel and the widget
#      grid's ClusterSmallGrid column reading, and runs it over the committed
#      fixtures — comparing every drawn cell against the cells
#      ios-app/scripts/cluster_monitor_cli_parity.py generated from
#      hscc_daemon/cluster_live.py's own host_table/serving_table. This is the
#      same shape as ios-app/scripts/model_decode_check.sh, and the reason the
#      reading file is Foundation-only.
#
#   2. STRUCTURAL. The split the executed half depends on: the view file must
#      not format a number, must not re-declare the section header, and must not
#      be a second design system. Grep legs, printed with the same verdict.
#
# Compile only, on the macOS SDK, no simulator and no device (the repo's
# standing rule). The harness lives in scripts/cli_dropdown_check/main.swift as
# a committed file — a heredoc would make the harness itself the thing that can
# silently drift.
#
# Usage: bash macos-app/scripts/cli_dropdown_check.sh   (from any cwd)
set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SELF_DIR/.." || exit 1

export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
SDK=$(xcrun --sdk macosx --show-sdk-path 2>/dev/null) || {
  echo "error: no macOS SDK (is Xcode installed?)" >&2; exit 1; }

FIXDIR="$(cd "$SELF_DIR/../../ios-app/scripts/model_decode_check/fixtures" 2>/dev/null && pwd)"
if [ -z "$FIXDIR" ] || [ ! -d "$FIXDIR" ]; then
  # The goldens are the iOS decode harness's; this gate re-uses them rather
  # than keeping a second copy that can drift.
  echo "error: fixture dir not found at ../ios-app/scripts/model_decode_check/fixtures" >&2
  exit 1
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

real_sources=(
  ../ios-app/Sources/Shared/SharedModels.swift
  ../ios-app/Sources/Shared/SharedStore.swift
  ../ios-app/Sources/Shared/ClusterMonitorViewModel.swift
  ../ios-app/Sources/HSCCWidgets/ClusterSmallGrid.swift
  Sources/HSCCMac/DashboardFleetReading.swift
)
harness=(
  ../ios-app/scripts/model_decode_check/ThemeStub.swift
  scripts/cli_dropdown_check/main.swift
)

echo "compiling the shipped dropdown layer + harness into a macOS CLI..."
if ! xcrun --sdk "$SDK" swiftc -o "$TMP/cli_dropdown_check" \
     "${real_sources[@]}" "${harness[@]}" 2>"$TMP/compile.err"; then
  echo "error: failed to compile the dropdown layer — see below" >&2
  cat "$TMP/compile.err" >&2
  exit 1
fi
if [ -s "$TMP/compile.err" ]; then
  echo "error: the dropdown layer compiled with diagnostics:" >&2
  cat "$TMP/compile.err" >&2
  exit 1
fi

"$TMP/cli_dropdown_check" "$FIXDIR"
executed=$?

# ---------------------------------------------------------------------------
# Structural half. Every leg prints ok:/FAIL and bumps ONE counter, so the
# number a passing run prints is measured from the legs that ran — never a
# literal typed in this file (a transcribed count goes stale on the first leg
# added, and reads as coverage while checking nothing).
# ---------------------------------------------------------------------------
grepfail=0
struct=0
view=Sources/HSCCMac/DashboardFleetSection.swift
reading=Sources/HSCCMac/DashboardFleetReading.swift

ok()   { struct=$((struct + 1)); echo "ok   $1"; }
bad()  { struct=$((struct + 1)); grepfail=$((grepfail + 1)); echo "FAIL $1"; }

# (1) The view formats nothing. Every number the pane shows is the CLI's cell,
#     so a String(format:) here would be a second rounding rule.
if grep -qn 'String(format:' "$view"; then
  bad "$view formats a number (String(format:) — the CLI's cells are the only source)"
else
  ok "$view formats no number"
fi

# (2) No raw colour: the one Theme palette, so the dropdown cannot drift from
#     the widgets and the app.
if grep -qnE 'Color\.(red|green|orange|blue|yellow|primary|secondary|white|black)' "$view"; then
  bad "$view names a raw colour instead of Theme.Semantic"
else
  ok "$view colours only through Theme.Semantic"
fi

# (3) One section-header style: the view reuses DashboardView's, it does not
#     re-declare one.
if grep -qn 'func sectionTitle' "$view"; then
  bad "$view re-declares sectionTitle (one header style lives in DashboardView)"
else
  ok "$view reuses DashboardView's sectionTitle"
fi

# (4) The reading layer stays Foundation-only — that is what makes half 1 of
#     this gate possible, and nothing in the app's own compile would notice.
#     Comment lines are excluded: this file's own header explains the rule in
#     prose that names the very types the leg forbids.
if grep -vE '^[[:space:]]*//' "$reading" | grep -qE '^import (SwiftUI|WidgetKit|AppKit|ActivityKit)|Color'; then
  bad "$reading is no longer Foundation-only (this gate could not compile it)"
else
  ok "$reading is Foundation-only"
fi

# (5) Wired into the target: build_check.sh parses these same - path: lines, so
#     an unlisted file is simply never compiled (and never shipped).
for leaf in DashboardFleetReading.swift DashboardFleetSection.swift ClusterSmallGrid.swift; do
  if grep -qn "$leaf" project.yml; then
    ok "project.yml compiles $leaf"
  else
    bad "project.yml does not list $leaf — the target never compiles it"
  fi
done

# (6) The pane shows no address (§7): the fixture half proves the labels, this
#     leg keeps the view from drawing the raw wire value beside them.
if grep -qn 'Text(row\.host' "$view"; then
  bad "$view draws a raw wire host value (draw the display label, §7)"
else
  ok "$view draws display labels only"
fi

echo ""
if [ "$grepfail" -gt 0 ] || [ "$executed" -ne 0 ]; then
  echo "CLI DROPDOWN CHECK FAILED — executed half rc=$executed, $grepfail structural check(s) failed"
  exit 1
fi
echo "CLI DROPDOWN CHECK PASSED — executed half green + $struct structural checks"
