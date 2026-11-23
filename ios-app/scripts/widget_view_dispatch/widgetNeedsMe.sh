#!/bin/bash
# widget_view_dispatch/widgetNeedsMe.sh — the NEEDS ME widget surface
# (WidgetSurfaceKind.widgetNeedsMe, card R3).
#
# Subject: Sources/HSCCWidgets/NeedsMeWidget.swift — six families that can draw
# live alert rows, so the gate count below is 6.
#
# Same claims as the other registry-driven widgets: every family gates on BOTH
# states in one step, the paused copy has one code owner, the accessory bodies
# draw the entry's shortGateLabel and that copy decides !configured before any
# paused text, cadence and pause come from the shared registry, the paused entry
# comes from the entry's own factory, paused is decided before the client exists,
# and the cadence function is sliced out of the committed source and executed.
#
# Run standalone, or through the aggregator:
#   ./ios-app/scripts/widget_view_dispatch/widgetNeedsMe.sh
set -uo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"
SDK=$(wvd_sdk) || exit 1

widget_surface_gate \
  Sources/HSCCWidgets/NeedsMeWidget.swift \
  needs_me_cadence \
  ".paused(refreshMinutes:" \
  6 \
  '/private func fetchEntry\(surface:/ {f=1} f {print} f && /^    \}$/ {exit}' \
  widgetNeedsMe

wvd_finish
