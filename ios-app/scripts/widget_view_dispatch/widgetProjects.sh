#!/bin/bash
# widget_view_dispatch/widgetProjects.sh — the PROJECTS widget surface
# (WidgetSurfaceKind.widgetProjects).
#
# Subject: Sources/HSCCWidgets/ProjectsWidget.swift. Same claims as
# widgetKanbanMonitor.sh (see common.sh for widget_surface_gate), except that
# this widget ships no accessory family at all: the 7th argument is
# home-screen-only, which swaps the two label checks for their INVERSE —
# supportedFamilies must name no accessory family, and no shortGateLabel may
# exist. That is what makes the entry's missing label rule a proven absence
# rather than an oversight that goes false the day someone adds .accessoryCorner.
#
# Moved verbatim out of widget_view_dispatch_check.sh (lines 365-378 at c270ba84)
# by t_2c22e60d. Run standalone, or through the aggregator:
#   ./ios-app/scripts/widget_view_dispatch/widgetProjects.sh
set -uo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"
SDK=$(wvd_sdk) || exit 1

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

wvd_finish
