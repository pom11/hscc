#!/bin/bash
# model_decode_check.sh — decode every committed fixture against the REAL models.
#
# Unlike the old mirrored validator (which re-declared every struct and could
# drift into a false green), this compiles the ACTUAL model sources into a
# plain macOS CLI and decodes every committed fixture with them. A decode
# mismatch means the real Models.swift / SharedModels.swift no longer match the
# live JSON — the exact false-green this tool exists to prevent.
#
# What it compiles (all real, none redeclared here):
#   Sources/HSCC/Models.swift
#   Sources/Shared/SharedModels.swift
#   Sources/Shared/SharedStore.swift
#   Sources/HSCC/APIError.swift
# The only non-model shim (ThemeStub.swift — a UI design token, not a model)
# exists because SharedModels.swift references Theme.Semantic.* colors, which on
# macOS render nothing but satisfy the needed types.
#
# SharedStore.swift is in the list because §7 binds the STORED shape, not the
# decoded one: `MonitorNodeEntry` + `MonitorSnapshotStore.project` + `Snapshot`
# are production types here, so the §7 monitor-snapshot assertions at the end of
# main.swift check the bytes the App Group would actually hold. It is Foundation
# + Security only, so it compiles for macOS like the models do.
#
# Runs on macOS only — there is no iOS platform runtime on this host, and the
# decode logic is pure Foundation (the model files are iOS-Codable but decode
# anywhere), so a macOS CLI is the faithful fixture runner.
#
# Usage: scripts/model_decode_check.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
SDK=$(xcrun --sdk macosx --show-sdk-path 2>/dev/null) || {
  echo "error: no macOS SDK (is Xcode installed?)" >&2; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

real_models=(
  Sources/HSCC/Models.swift
  Sources/Shared/SharedModels.swift
  Sources/Shared/SharedStore.swift
  Sources/HSCC/APIError.swift
  # The monitor view-model (t_61ed3f5e): the parity checks below run THIS
  # file's formatters against goldens generated from hscc_daemon/cluster_live.py,
  # so the harness has to compile the real thing, not a copy of it.
  Sources/Shared/ClusterMonitorViewModel.swift
  # The systemSmall grid (t_9e110d88): Foundation-only by design, so the tile
  # the small widget draws is executable here — the same bytes the view renders,
  # no simulator. Its SwiftUI half lives in ClusterSmallGridView.swift and is
  # covered structurally by widget_style_check.sh / cluster_widget_check.sh.
  Sources/HSCCWidgets/ClusterSmallGrid.swift
  # The large/XL table layout (t_9e64d9d9): which columns and rows the two
  # biggest monitor surfaces draw, and the six fields only they print. Layout
  # lives outside the view files so this harness can RUN it. It names
  # `FleetSurface` and `FleetFormat.dash`, so the file that owns them is
  # compiled here too — a harness that skipped a dependency would either not
  # compile or, worse, test a copy of the rule.
  Sources/Shared/ClusterNodeTally.swift
  Sources/Shared/ClusterMonitorTables.swift
  # The Live Activity's surface rules (t_1685f884): Foundation-only on purpose,
  # so the checks below run the functions the shipped surfaces call. If this
  # file ever needs SwiftUI, the rules have leaked into it — move them out
  # rather than stubbing a UI framework here.
  Sources/Shared/MonitorActivityMetrics.swift
  # The alert-feed projection ("Needs me", card R3): the decode gate runs the
  # same newest-first order, unacknowledged filter, age and body-first-line
  # rules the widget and the macOS dropdown draw.
  Sources/Shared/NeedsMeReading.swift
)
harness=(
  scripts/model_decode_check/ThemeStub.swift
  # Declarations only (Swift forbids top-level statements outside main.swift),
  # so the monitor parity checks live here and main.swift calls them.
  scripts/model_decode_check/monitor_parity.swift
  scripts/model_decode_check/cluster_small_grid.swift
  scripts/model_decode_check/monitor_tables.swift
  scripts/model_decode_check/monitor_surfaces.swift
  scripts/model_decode_check/main.swift
)

echo "compiling real models + harness into a macOS CLI..."
if ! xcrun --sdk "$SDK" swiftc -o "$TMP/decode_check" \
     "${real_models[@]}" "${harness[@]}" 2>"$TMP/compile.err"; then
  echo "error: failed to compile the real model sources — see below" >&2
  cat "$TMP/compile.err" >&2
  exit 1
fi

FIXDIR=$(cd scripts/model_decode_check/fixtures && pwd)
"$TMP/decode_check" "$FIXDIR"
exit $?
