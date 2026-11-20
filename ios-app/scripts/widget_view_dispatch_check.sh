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
#
# (t_2c22e60d) THIS FILE RUNS NO CHECKS. Every check lives in exactly one file
# per widget surface, under widget_view_dispatch/, and this file only iterates
# the manifest below, runs each surface, and aggregates pass/fail. The split
# exists because the pre-split file was ONE append-only program that every
# widget-registry card added its own numbered checks to: PR #341 and PR #344
# collided in it by construction, and a conflict in a test file is the one most
# likely to be resolved by taking the wrong side.
#
# ADDING A SURFACE: create widget_view_dispatch/<WidgetSurfaceKind case>.sh — one
# file per registry case, checks in it, helpers in common.sh (never re-declared) —
# then add its name to MANIFEST. That is the whole change. Never append a check
# to this file. The orphan guard below fails the run if a surface file exists
# without a manifest line, so a check can never be added and silently not run.
#
# CLI contract and exit codes are the pre-split ones: no arguments, 0 when every
# check passed, 1 when any check failed. scripts/run_tests.sh and the release
# gate call it exactly as before, and a fully green run prints byte-identical
# output to the pre-split harness (see references/check-inventory.md).
set -uo pipefail

# Resolve this file's own directory BEFORE common.sh cd's (see the note there).
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECK_DIR="$SELF_DIR/widget_view_dispatch"
. "$CHECK_DIR/common.sh"

# The manifest: one entry per widget surface the harness covers, in the order
# they ran before the split. Deliberately a list of names and nothing else — no
# check counts anywhere, because a transcribed count reddens a healthy gate the
# day a neighbour adds a check (the trap widget_settings_check.sh shipped with
# `surfaces.count == 4`).
MANIFEST=(
  widgetCluster
  widgetBoardStatus
  widgetKanbanMonitor
  widgetCardsRunning
  widgetProjects
  widgetNeedsMe
  # Appended, not inserted: the list stays the order the surfaces ran in, and a
  # new surface joins at the end so two cards adding one cannot fight over the
  # middle of the array.
  widgetMergeQueue
)

# One xcrun call for the whole run instead of one per surface, so a machine
# without the SDK still produces exactly one "no macOS SDK" line, as before.
WVD_SDK=$(wvd_sdk) || exit 1
export WVD_SDK

overall=0

for surface in "${MANIFEST[@]}"; do
  check="$CHECK_DIR/$surface.sh"
  if [ ! -f "$check" ]; then
    echo "  FAIL: $surface is in the manifest but $check does not exist"
    overall=1
    continue
  fi
  # Output streams straight through, so the aggregate run's stdout is the
  # concatenation of the surface outputs byte for byte. A surface that exits
  # non-zero — by a printed FAIL or by one of its own guards — is counted.
  bash "$check" || { echo "surface $surface failed" >&2; overall=1; }
done

# Orphan guard: a surface file nobody listed in the manifest would never run.
# That is the silent-drop failure mode this split must not introduce, so an
# unlisted file is a red run, not an ignored file. (common.sh is the one
# non-surface file in the directory.)
for check in "$CHECK_DIR"/*.sh; do
  name=$(basename "$check" .sh)
  [ "$name" = common ] && continue
  known=0
  for surface in "${MANIFEST[@]}"; do
    [ "$surface" = "$name" ] && known=1
  done
  if [ "$known" -eq 0 ]; then
    echo "  FAIL: $check is not in the aggregator manifest — its checks would never run"
    overall=1
  fi
done

# macOS desktop target (t_1143c19b). The extension does not own a second copy
# of any surface: `../macos-app/project.yml` lists these exact files by path,
# so every structural check above governs the desktop widget too. This block
# proves the reuse is real rather than a claim in a comment — every widget
# source on disk must be in the macOS target's source list, and the target must
# compile the one shared App Group store + client + style bridge.
MAC_SPEC="../macos-app/project.yml"
if [ -f "$MAC_SPEC" ] && grep -q '^  HSCCMacWidgets:' "$MAC_SPEC"; then
  mac_missing=0
  # The whole widget directory is one computed source entry, so every widget
  # file on disk (including a newly added one) is compiled by the desktop
  # target without editing macos-app/project.yml.
  if ! grep -qF -- "- path: ../ios-app/Sources/HSCCWidgets" "$MAC_SPEC"; then
    echo "  FAIL: macOS target does not compile ../ios-app/Sources/HSCCWidgets — a desktop surface would be a second copy"
    mac_missing=1
  fi
  for shared in Sources/Shared/SharedStore.swift Sources/Shared/WidgetSurfaceKind.generated.swift \
                Sources/Shared/WidgetDataClient.swift Sources/HSCC/Views/Theme.swift; do
    if ! grep -qF -- "- path: ../ios-app/$shared" "$MAC_SPEC"; then
      echo "  FAIL: macOS target does not compile $shared — the desktop widget would have a second data path"
      mac_missing=1
    fi
  done
  [ "$mac_missing" -eq 0 ] && echo "  ok: HSCCMacWidgets reuses every widget surface + the shared store/client/style by path"
  [ "$mac_missing" -eq 0 ] || overall=1
else
  echo "  FAIL: $MAC_SPEC has no HSCCMacWidgets target — the macOS desktop widgets are missing"
  overall=1
fi

if [ "$overall" -ne 0 ]; then
  # Pre-split, this banner was driven by the widget-surface counter alone, so a
  # board-status FAIL still printed PASSED and only then exited 1 — a red check
  # wearing a green banner, which is the exact shape this harness exists to
  # catch. It now covers every surface in the manifest.
  echo "WIDGET SURFACE GATE CHECK FAILED — a registry-driven surface can draw data it was told not to fetch"
  exit 1
fi
echo "WIDGET SURFACE GATE CHECK PASSED — every registry-driven widget body gates paused and unconfigured in one step, decides pause before the client exists, and its cadence is executed off the committed source"

exit 0
