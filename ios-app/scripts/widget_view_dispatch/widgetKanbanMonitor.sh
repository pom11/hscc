#!/bin/bash
# widget_view_dispatch/widgetKanbanMonitor.sh — the KANBAN MONITOR widget surface
# (WidgetSurfaceKind.widgetKanbanMonitor).
#
# Subject: Sources/HSCCWidgets/KanbanMonitorWidget.swift — six families that can
# draw live data, so the gate count below is 6.
#
# The claims are the ones widget_surface_gate encodes (see common.sh, and
# references/check-inventory.md for the expanded body): every family gates on
# BOTH states in one step, the paused copy has exactly one code owner, the
# accessory bodies draw the entry's shortGateLabel and that copy decides
# !configured before any paused text, cadence and pause come from the shared
# registry, the paused entry comes from the entry's own factory, paused is
# decided before the client exists, and the cadence function is sliced out of the
# committed source by an END-ANCHORED GATE-SLICE marker and executed.
#
# Moved verbatim out of widget_view_dispatch_check.sh (lines 344-350 at c270ba84)
# by t_2c22e60d. Run standalone, or through the aggregator:
#   ./ios-app/scripts/widget_view_dispatch/widgetKanbanMonitor.sh
set -uo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"
SDK=$(wvd_sdk) || exit 1

widget_surface_gate \
  Sources/HSCCWidgets/KanbanMonitorWidget.swift \
  kanban_monitor_cadence \
  ".paused(refreshMinutes:" \
  6 \
  '/private func fetchEntry\(surface:/ {f=1} f {print} f && /^    \}$/ {exit}' \
  widgetKanbanMonitor

wvd_finish
