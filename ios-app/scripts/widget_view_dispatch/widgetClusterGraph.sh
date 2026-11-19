#!/bin/bash
# widget_view_dispatch/widgetClusterGraph.sh — the CLUSTER GRAPH widget surface
# (WidgetSurfaceKind.widgetClusterGraph), t_f4551339 / PR #426.
#
# Subjects, and why this file names one per check rather than one overall:
#   Sources/HSCCWidgets/GraphWidget.swift        provider, cadence slice,
#                                                registry read, supportedFamilies
#   Sources/HSCCWidgets/GraphWidgetEntry.swift   the entry's one gate copy
#   Sources/HSCCWidgets/GraphWidgetViews.swift   the three gated family bodies
#   Sources/HSCCWidgets/GraphWidgetMetrics.swift the family -> density map
# The other registry surfaces keep provider + entry + bodies in a single file and
# so can hand widget_surface_gate one path; this widget cannot, which is the only
# reason common.sh now exposes the checks individually. Run standalone, or
# through the aggregator:
#   ./ios-app/scripts/widget_view_dispatch/widgetClusterGraph.sh
set -uo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"
SDK=$(wvd_sdk) || exit 1

W=Sources/HSCCWidgets
PROVIDER="$W/GraphWidget.swift"
ENTRY="$W/GraphWidgetEntry.swift"
VIEWS="$W/GraphWidgetViews.swift"
METRICS="$W/GraphWidgetMetrics.swift"
GRAPH_GATE=scripts/cluster_graph_check.sh

# (3) Home-screen-only. The widget declares systemSmall/Medium/Large/ExtraLarge
# through the positive list in GraphWidgetMetrics.swift and has NO accessory
# family, so the families rule runs in its inverse form: supportedFamilies names
# no `.accessory*`, and the label absence below is proven rather than assumed.
# Never add a `shortGateLabel` to this widget "for symmetry" — with no
# complication to render it, the string is copy no body can reach, and this call
# fails it by name.
widget_gate_families "$PROVIDER" home-screen-only

# ...and the half of that rule its literal grep cannot reach. This widget hands
# `supportedFamilies` the named list from GraphWidgetMetrics.swift, so the file has
# never contained a `supportedFamilies([...])` literal for that grep to look at — an
# `ok` from a pattern that cannot match is a check wearing a comment, and it has been
# printing since the surface was registered. The claim itself (no accessory family
# ANYWHERE in the widget, list resolved, all four shipped files) belongs to
# cluster_graph_check.sh §2, so it is delegated and the delegation is tested: delete
# the rule there and this file reddens by name instead of the rule quietly orphaning.
widget_gate_rule_has_owner "$GRAPH_GATE" '\.accessory' \
  "no accessory family anywhere in the graph widget (home-screen-only)"

# (3, inverse) and the same absence in the three files that do NOT declare
# supportedFamilies: a label added later to the entry or to a view is exactly the
# drift this rule refuses, and one call per shipped file is what catches it.
widget_gate_no_short_gate_label "$ENTRY"
widget_gate_no_short_gate_label "$VIEWS"
widget_gate_no_short_gate_label "$METRICS"

# (4) The cadence and the pause come from the shared registry, so the Settings
# row for this surface is the only write path. cluster_graph_check.sh proves the
# surface HAS a settings key in SharedStore; nothing proved the widget READS it
# until this line — a widget can hold a key it never consults and then ignore a
# pause someone can see in Settings.
widget_gate_registry_key "$PROVIDER" widgetClusterGraph

# (5) The paused entry is built by the entry itself, naming its one paused case.
# There is no `.paused(...)` factory here and none is being asked for:
# ClusterGraphEntry.status is a single value, so `status: .paused` IS the entry's
# way of saying paused, and the claim this check makes is that the provider uses
# it rather than projecting data into a paused pane.
widget_gate_paused_factory "$PROVIDER" "status: .paused"

# (2) The paused sentence has exactly one owner, and it is the entry's `gateLine`.
# Note where the file points: for this widget the copy is not in the provider's
# file, which is the whole reason gate (2) is callable on its own now.
widget_gate_one_paused_copy "$ENTRY"

# (7) The cadence, EXECUTED off the committed slice — the only branch of the
# provider no compile can reach: Off and a corrupt negative must both yield no
# next date, or a paused surface turns into a refresh loop.
widget_gate_cadence "$PROVIDER" cluster_graph_cadence

# ---------------------------------------------------------------------------
# (1) and (6) are deliberately NOT measured here, and the delegation is tested
# rather than asserted. Both rules belong to cluster_graph_check.sh, which
# slices the provider's own entry function and orders the decisions inside it;
# copying either claim here would give one rule two owners, which is the failure
# this fleet's gates have already paid for once.
#
# (1) The one-step gate over the three bodies (a body that tests `configured`
#     and then separately `paused` is a body that forgets one).
# (6) The order inside the fetch. This widget ships INVITE before PAUSE, which
#     is the inverse of the wording in widget_surface_gate's own check: because
#     ClusterGraphEntry.status carries ONE value, `configured` and `paused`
#     cannot both be true, so whichever guard fires first decides the sentence
#     the operator reads. Ask the paused question first and a device that lost
#     its token while the surface was switched off prints "Paused in Settings"
#     forever, hiding the one line that would tell someone to configure the app.
#     What the fleet ordering really protects — a paused surface issues zero
#     requests — is measured there as "the pause is decided before any history
#     request", and it holds either way round. Changing the widget to match a
#     copied assertion would be the bug, not the fix.
# ---------------------------------------------------------------------------
widget_gate_rule_has_owner "$GRAPH_GATE" \
  'if !entry.configured || entry.paused {' \
  "the one-step gate over the three graph bodies"
widget_gate_rule_has_owner "$GRAPH_GATE" \
  '[ "$pause_at" -lt "$read_at" ]' \
  "the graph provider's invite-before-pause-before-read order"

wvd_finish
