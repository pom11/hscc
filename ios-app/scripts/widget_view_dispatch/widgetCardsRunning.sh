#!/bin/bash
# widget_view_dispatch/widgetCardsRunning.sh — the RUNNING CARDS widget surface
# (WidgetSurfaceKind.widgetCardsRunning).
#
# Subject: Sources/HSCCWidgets/CardsRunningWidget.swift. Same claims as
# widgetKanbanMonitor.sh (see common.sh for widget_surface_gate), and SIX
# families of its own: five bodies carry the one-step gate and the sixth is the
# entry's inline line, which check (3) measures on the entry.
#
# Moved verbatim out of widget_view_dispatch_check.sh (lines 352-363 at c270ba84)
# by t_2c22e60d. Run standalone, or through the aggregator:
#   ./ios-app/scripts/widget_view_dispatch/widgetCardsRunning.sh
set -uo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"
SDK=$(wvd_sdk) || exit 1

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

wvd_finish
