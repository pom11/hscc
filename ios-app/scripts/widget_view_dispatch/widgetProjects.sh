#!/bin/bash
# widget_view_dispatch/widgetProjects.sh — the PROJECTS widget surface
# (WidgetSurfaceKind.widgetProjects).
#
# Subject: Sources/HSCCWidgets/ProjectsWidget.swift. Same claims as
# widgetKanbanMonitor.sh (see common.sh for widget_surface_gate), and SIX
# families of its own: five bodies carry the one-step gate (small, medium,
# large, circular, rectangular) and the sixth is the entry's inline line, which
# check (3) measures on the entry through `shortGateLabel`.
#
# Moved verbatim out of widget_view_dispatch_check.sh (lines 365-378 at c270ba84)
# by t_2c22e60d. Run standalone, or through the aggregator:
#   ./ios-app/scripts/widget_view_dispatch/widgetProjects.sh
set -uo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"
SDK=$(wvd_sdk) || exit 1

# The projects widget (t_5f246177, t_fece1b58): six families now that the
# operator can put it on the Lock Screen, so its entry owns the shortGateLabel
# rule the accessory mode measures. Five gated bodies: the three Home Screen
# sizes plus circular and rectangular; inline routes through `inlineLine`.
widget_surface_gate \
  Sources/HSCCWidgets/ProjectsWidget.swift \
  projects_cadence \
  ".paused(selectedName:" \
  5 \
  '/private func fetch\(for configuration:/ {f=1} f {print} f && /^    \}$/ {exit}' \
  widgetProjects

wvd_finish
