#!/bin/bash
# cluster_graph_check.sh — execute the history graph widget's projection layer
# and hold its structure to what the card and the fleet's gates require.
#
# Subject: ios-app/Sources/Shared/ClusterGraphReading.swift (t_f4551339) — the
# rules that decide what each Home Screen size DRAWS from a
# GET /v1/cluster/history answer. Like cluster_history_chart_check.sh this
# compiles the REAL source plus the REAL wire types into a plain macOS CLI and
# runs it: no redeclaration, no network, no simulator, no WidgetKit.
#
# WHY A GATE OF ITS OWN. build_check.sh proves the widget compiles and
# model_decode_check.sh proves the payload decodes. Neither can notice that a
# panel put GPU% and °C on one axis (eight metrics, four units), that a
# 40-pixel draw dropped the spike the operator opens the widget to see, that a
# window with no reading got joined into a straight line, that a 7-day chart
# printed a 2-second claim, or that a missing value got drawn as a zero. A chart
# lies by SHAPE, which is precisely what no shape-checking gate sees.
#
# THE STRUCTURAL LEG exists because the four family bodies cannot be executed on
# macOS at all: WidgetKit's `WidgetFamily` is Int-backed and its accessory cases
# are `@available(macOS, unavailable)`, so the family → density map is written as
# a POSITIVE LIST in Sources/HSCCWidgets/GraphWidgetMetrics.swift. The harness
# asserts the shared half of that map's vocabulary; this leg asserts the
# WidgetKit half — one dispatch arm per declared family (the t_b3ae3809 failure,
# where a declared systemLarge silently drew the medium layout), no accessory
# family anywhere (widget_view_dispatch gates this surface as home-screen-only),
# the canonical-route rule of DECISION-t_447cc7fc, and the registry wiring that
# makes the Settings row able to pause and pace this widget at all.
#
# NEGATIVE TEST (guarded): break the layer deliberately and confirm the harness
# FAILS. A harness that cannot fail is worthless:
#   export CLUSTER_GRAPH_NEGATIVE_TEST=1
#
# Usage: ios-app/scripts/cluster_graph_check.sh

set -uo pipefail
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
cd "$(dirname "${BASH_SOURCE[0]}")/.."

export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
SDK=$(xcrun --sdk macosx --show-sdk-path 2>/dev/null) || {
  echo "error: no macOS SDK (is Xcode installed?)" >&2; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

subject="Sources/Shared/ClusterGraphReading.swift"
real_models=(
  Sources/Shared/SharedModels.swift
  Sources/Shared/SharedStore.swift
  Sources/Shared/ClusterHistorySeries.swift
  Sources/Shared/ClusterMonitorViewModel.swift
)
harness="scripts/cluster_graph_check/main.swift"
theme_stub="scripts/model_decode_check/ThemeStub.swift"
fixture_dir="scripts/model_decode_check/fixtures"

widget_dir="Sources/HSCCWidgets"
widget_files=(
  "$widget_dir/GraphWidget.swift"
  "$widget_dir/GraphWidgetEntry.swift"
  "$widget_dir/GraphWidgetMetrics.swift"
  "$widget_dir/GraphWidgetViews.swift"
)
ios_project="project.yml"
mac_project="../macos-app/project.yml"

for f in "$subject" "$harness" "$theme_stub"; do
  if [ ! -f "$f" ]; then echo "error: $f missing — did it get moved?" >&2; exit 1; fi
done
for f in "${real_models[@]}"; do
  if [ ! -f "$f" ]; then echo "error: $f missing — the harness can no longer build the real model" >&2; exit 1; fi
done
for f in "${widget_files[@]}"; do
  if [ ! -f "$f" ]; then echo "error: $f missing — the widget this gate guards is gone" >&2; exit 1; fi
done
for f in v1_cluster_history_6h.json v1_cluster_history_7d.json v1_cluster_history_6h_engine_only.json v1_cluster_history_15m.json; do
  if [ ! -f "$fixture_dir/$f" ]; then
    echo "error: $fixture_dir/$f missing — regenerate with scripts/make_cluster_history_fixture.py" >&2
    exit 1
  fi
done

# The harness must drive the REAL layer; if these markers move it is testing a
# copy, which is the false-green these harnesses exist to prevent.
if ! grep -q "ClusterGraphReading.panels" "$harness" ||
   ! grep -q "ClusterGraphReading.envelope" "$harness" ||
   ! grep -q "ClusterGraphReading.minis" "$harness"; then
  echo "error: harness no longer drives the real projection layer (markers moved?)" >&2
  exit 1
fi
if ! grep -q "Sources/Shared/ClusterGraphReading.swift" "$SELF"; then
  echo "error: runner no longer compiles the real projection layer (marker moved?)" >&2
  exit 1
fi

echo "compiling the REAL projection layer + wire models + view-model + harness into a macOS CLI..."
if ! xcrun --sdk "$SDK" swiftc -o "$TMP/cluster_graph_check" \
     "$subject" "${real_models[@]}" "$theme_stub" "$harness" 2>"$TMP/compile.err"; then
  echo "error: failed to compile the graph projection layer — see below" >&2
  cat "$TMP/compile.err" >&2
  exit 1
fi
# A warning here is a finding: build_check.sh holds every target to 0 warnings,
# and the same source compiles into all of them.
if [ -s "$TMP/compile.err" ]; then
  echo "error: the projection layer compiled with diagnostics — treated as findings:" >&2
  cat "$TMP/compile.err" >&2
  exit 1
fi

echo "running the projection checks against the route's own fixtures..."
"$TMP/cluster_graph_check" "$PWD/$fixture_dir"
harness_rc=$?

# ---------------------------------------------------------------------------
# Structural leg. Each check prints one line and says which file it read, so a
# failure points at the file to fix rather than at this script.
#
# WHY THE CHECKS READ FROM VARIABLES, NOT PIPES. This script runs under
# `set -o pipefail`, and `grep -q` exits the MOMENT it matches: sed then dies of
# SIGPIPE with status 141, pipefail reports 141, and `if sed … | grep -q X` takes
# the ELSE branch on a file that contains X. That is not hypothetical — this leg
# was wrong in exactly that way and reported "drawSegments vanished" for a file
# that has it on line 385, while the same pattern passed on a 300-line file whose
# match sat near EOF. So every file is read ONCE into a variable and the greps run
# against a here-string, where there is no pipe for anyone to break. A short
# helper (`has`) keeps that readable.
# ---------------------------------------------------------------------------
echo ""
echo "structural leg: the WidgetKit half the macOS harness cannot execute..."

struct_fail=0
sok() { echo "  ok: $1"; }
sfail() { echo "  FAIL: $1"; }
# `has PATTERN TEXT` — true when TEXT contains PATTERN (BRE). No pipe, so a
# match cannot be turned into a failure by an early grep exit.
has() { grep -q -- "$2" <<<"$1"; }
hasnt() { ! grep -q -- "$2" <<<"$1"; }

families_file="$widget_dir/GraphWidgetMetrics.swift"
views_file="$widget_dir/GraphWidgetViews.swift"
intent_file="$widget_dir/GraphWidget.swift"
entry_file="$widget_dir/GraphWidgetEntry.swift"
reading="Sources/Shared/ClusterGraphReading.swift"
layer="Sources/Shared/ClusterHistorySeries.swift"
client="Sources/Shared/WidgetDataClient.swift"
store="Sources/Shared/SharedStore.swift"
bundle="Sources/HSCCWidgets/HSCCWidgetsBundle.swift"
labels="Sources/HSCC/Views/WidgetSurfaceLabels.swift"

# Comment text is stripped before any code-pattern search. Without that, a
# sentence saying "no accessory family is declared" trips the accessory
# assertion and a doc comment naming the retired path trips the path assertion:
# the gate then fails on PROSE, and a gate that fails on prose gets silenced by
# the next person rather than fixed.
code_text() { sed -e 's://[^"]*::' -e '/^[[:space:]]*$/d' "$@"; }

reading_code="$(code_text "$reading")"
layer_code="$(code_text "$layer")"
client_code="$(code_text "$client")"
store_code="$(code_text "$store")"
bundle_code="$(code_text "$bundle")"
labels_code="$(code_text "$labels")"
families_code="$(code_text "$families_file")"
views_code="$(code_text "$views_file")"
intent_code="$(code_text "$intent_file")"
widget_code="$(code_text "${widget_files[@]}")"

# 1. One dispatch arm per declared family. This is the t_b3ae3809 failure: a
#    family in `supportedFamilies` whose body was reached through `default:`
#    drew another size's layout, and nothing in a compile or a decode could see
#    it. The list is read from the code, never transcribed here.
declared=$(awk '/static let clusterGraphFamilies/,/^    \]/' <<<"$families_code" \
  | grep -o '\.system[A-Za-z]*' | sed 's/^\.//' | sort -u)
if [ -z "$declared" ]; then
  sfail "no family list found in $families_file — the positive list is gone"
else
  for fam in $declared; do
    if has "$views_code" "case .$fam:"; then
      sok "$fam has its own arm in $views_file"
    else
      sfail "$fam is declared but has no dispatch arm in $views_file (it would draw via default:)"
    fi
  done
fi
fam_count=$(printf '%s\n' $declared | sed '/^$/d' | wc -l | tr -d ' ')
if [ "$fam_count" -eq 4 ]; then
  sok "the widget declares the card's four sizes (small, medium, large, extraLarge)"
else
  sfail "the widget declares $fam_count sizes, not the card's four"
fi

# 2. Home-screen-only. No accessory family anywhere: the card asks for Home
#    Screen sizes, `widget_view_dispatch` gates this surface as home-screen-only,
#    and WidgetFamily's accessory cases are `@available(macOS, unavailable)` — so
#    adding one means an availability guard, a label rule, and switching the
#    dispatch mode, not just adding a case. (The positive list above is what makes
#    that true on both platforms: all four families exist on macOS, which is why
#    this file needs no `#if os(iOS)` around the list itself.)
if has "$widget_code" "\.accessory"; then
  sfail "an .accessory family appears in the graph widget: this surface is gated home-screen-only"
else
  sok "no accessory family anywhere in the graph widget (home-screen-only holds)"
fi
if has "$intent_code" "supportedFamilies(WidgetFamily.clusterGraphFamilies)"; then
  sok "supportedFamilies reads the same positive list the dispatch is checked against"
else
  sfail "supportedFamilies no longer reads WidgetFamily.clusterGraphFamilies (list and arms can drift)"
fi

# 3. The family → density map is a positive list written out per size, because
#    WidgetFamily is Int-backed off-iOS and the harness cannot execute this side.
if has "$families_code" "case .systemSmall: return .sparkline" \
   && has "$families_code" "case .systemMedium: return .nodeOverlay" \
   && has "$families_code" "case .systemLarge, .systemExtraLarge: return .smallMultiples" \
   && has "$families_code" "default: return nil"; then
  sok "family -> density is the card's map, written as a positive list"
else
  sfail "family -> density drifted from the card's map in $families_file"
fi
# The card asks for ONE PANEL PER NODE, so the panel count must not be capped by
# a transcribed constant: a `limit: 4` would quietly drop a real host from a
# nine-node fleet. If a cap is ever justified it has to be a decision that says
# why, and this check has to be edited with it.
if has "$reading_code" "var panelLimit: Int? { nil }"; then
  sok "small multiples are uncapped by design (ClusterGraphDensity.panelLimit says so)"
else
  sfail "the 'no panel cap' rule vanished from $reading — a cap must be a decision, not a leftover"
fi

# 4. The canonical route ruling (DECISION-t_447cc7fc): `/v1/metrics/history` is
#    RETIRED and every history read goes through WidgetDataClient.clusterHistory
#    on `/v1/cluster/history`. A grep is the right tool here precisely because the
#    retired path would compile and run perfectly well — this is a decision the
#    type system cannot hold. `MetricsHistoryTrend` is not edited on this card (a
#    separate card retires it), so the check is that nothing NEW reaches for it.
if has "$widget_code" "v1/metrics/history" || has "$widget_code" "MetricsHistoryTrend"; then
  sfail "a graph-widget file names the RETIRED /v1/metrics/history read (see DECISION-t_447cc7fc)"
else
  sok "no graph-widget file reaches the retired /v1/metrics/history"
fi
if has "$intent_code" "client.clusterHistory("; then
  sok "the widget's only history read is WidgetDataClient.clusterHistory"
else
  sfail "$intent_file no longer reads through WidgetDataClient.clusterHistory"
fi
if has "$client_code" "func clusterHistory"; then
  sok "the client method the widget calls exists on the canonical path"
else
  sfail "WidgetDataClient.clusterHistory is gone — the widget's read would silently stop working"
fi
if has "$client_code" "cluster/history"; then
  sok "the client itself still points at the canonical /v1/cluster/history path"
else
  sfail "the client no longer names /v1/cluster/history"
fi

# 5. The operator's amendment: the DEFAULT window is the 2-second tier, that
#    recommendation lives in the shared layer rather than as a picker literal, and
#    the resolution the ROUTE served is what gets printed (a 7-day chart must not
#    read like live 2-second data).
if has "$intent_code" "default: .fifteenMinutes"; then
  sok "the window parameter defaults to 15 m (the 2 s tier), as the operator ruled"
else
  sfail "the window parameter no longer defaults to 15 m in $intent_file"
fi
if has "$reading_code" "static let recommended: ClusterGraphWindow = .fifteenMinutes"; then
  sok "the 15 m default is a shared-layer decision, not a picker literal"
else
  sfail "ClusterGraphWindow.recommended is gone — the 2 s default now lives only in the UI"
fi
if has "$reading_code" "static func text(resolutionS: Int) -> String"; then
  sok "the served resolution has a caption rule the harness executes"
else
  sfail "ClusterGraphResolutionCaption.text vanished — the resolution would be inferred again"
fi
if has "$views_code" "ClusterGraphAxisRule.pixelColumns(measuredWidth:"; then
  sok "the reduction is driven by the view's MEASURED width, not a guessed one"
else
  sfail "$views_file no longer measures its own width — the pixel-column count would be invented"
fi
if has "$views_code" "chartYScale(domain: mini.axisFloor...mini.axisCeiling)"; then
  sok "every axis is drawn over the data's own domain"
else
  sfail "$views_file no longer uses the derived axis domain — an axis ceiling would be invented again"
fi
if hasnt "$widget_code" "0...100" && hasnt "$reading_code" "0...100"; then
  sok "no hard-coded axis range anywhere in the graph widget"
else
  sfail "a hard-coded 0...100 axis is back — a fleet that never left 40 C would draw a flat line"
fi
# A hole in the data must end the polyline. The reduction's own `segments()`
# cannot guarantee that (it computes breaks over the points it was handed, and
# when nothing needed reducing those still include the valueless samples whose
# timestamps step along at the tier interval), so the draw-time rule lives in the
# shared layer and the harness proves it. Both halves are checked here because a
# view that went back to raw points would pass the harness.
if has "$reading_code" "static func drawSegments(of line: ClusterGraphLine)"; then
  sok "the draw-time gap rule is in the shared layer, where the harness can execute it"
else
  sfail "drawSegments vanished from $reading — a hole would again be drawn as a slope"
fi
if has "$views_code" "ClusterGraphReading.drawSegments(of: line)"; then
  sok "the views draw through drawSegments, not through the reduction's raw points"
else
  sfail "$views_file draws raw points again"
fi

# 6. The pickers. If one offers a value the shared vocabulary or the route's
#    windows do not have, the widget sends a request the API answers with a 400
#    and the user sees an empty chart. Every extraction is asserted NON-EMPTY
#    before it is compared: a silent empty extraction would turn a check into a
#    pass, which is the one outcome worse than a failure.
option_block() { awk "/^enum $1: String, AppEnum {/,/^}/" <<<"$intent_code"; }

metric_set_cases=$(option_block ClusterMetricSetOption | grep -oE '^    case [a-z]+$' | awk '{print $2}' | tr '\n' ' ')
if [ -z "$metric_set_cases" ]; then
  sfail "could not read ClusterMetricSetOption's cases out of $intent_file"
else
  sok "picker metric sets:$metric_set_cases"
fi
metric_set_count=$(option_block ClusterMetricSetOption | grep -cE '^    case ')
if [ "${metric_set_count:-0}" -ne 5 ]; then
  sfail "the metric-set picker offers ${metric_set_count:-0} sets, not the card's five"
fi

window_pairs=$(option_block ClusterGraphWindowOption \
  | grep -oE '^    case [a-zA-Z0-9]+ = "[^"]+"' \
  | sed -E 's/^    case [a-zA-Z0-9]+ = "([^"]+)"/\1/' | tr '\n' ' ')
if [ -z "$window_pairs" ]; then
  sfail "could not read ClusterGraphWindowOption's raw values out of $intent_file"
else
  sok "picker window wire values:$window_pairs"
fi
window_count=$(option_block ClusterGraphWindowOption | grep -cE '^    case ')
if [ "${window_count:-0}" -ne 5 ]; then
  sfail "the window picker offers ${window_count:-0} windows: the card's four plus the operator's 15 m"
fi

# Each picker window must be a window the shared layer can send, with the SAME
# wire value — comparing the raw-value literals is what catches a picker that
# offers "30m" while `_parse_history_window` only accepts 15m/1h/6h/24h/7d.
wire_missing=""
for value in $window_pairs; do
  if ! has "$reading_code" "= \"$value\""; then
    wire_missing="$wire_missing $value"
  fi
done
if [ -z "$wire_missing" ]; then
  sok "every window the picker offers is a ClusterGraphWindow the route parses"
else
  sfail "picker windows with no shared-layer entry:$wire_missing"
fi

set_missing=""
for m in $metric_set_cases; do
  if ! has "$layer_code" "    case $m"; then
    set_missing="$set_missing $m"
  fi
done
if [ -z "$set_missing" ]; then
  sok "every metric set the picker offers exists in ClusterHistoryMetricGroup"
else
  sfail "picker sets with no shared vocabulary entry:$set_missing"
fi

# 7. The wiring that makes the Settings row reach this widget at all. A widget
#    that compiles but is missing from the bundle never appears in the gallery; a
#    surface with no settings key cannot be paused or paced, which the fleet's
#    settings contract requires of every configurable surface.
if has "$bundle_code" "ClusterGraphWidget()"; then
  sok "the graph widget is registered in HSCCWidgetsBundle"
else
  sfail "the graph widget is not in HSCCWidgetsBundle — it would never appear in the gallery"
fi
if has "$store_code" 'case widgetClusterGraph = "widget.clusterGraph"'; then
  sok "the surface has a settings key, so it can be paused and paced"
else
  sfail "the graph surface has no settings key in $store — the Settings row would be missing"
fi
if has "$labels_code" 'case .widgetClusterGraph: return "Cluster Graph Widget"'; then
  sok "the surface has a display label (the settings screen never prints a raw key or address)"
else
  sfail "the graph surface has no display label in $labels"
fi

# 8. The refresh policy comes from the surface settings, not from a literal:
#    `.atEnd` when the operator paused the surface, `.after(date)` otherwise. A
#    widget that hard-codes a cadence ignores a pause someone can see.
if has "$intent_code" "policy: .atEnd" && has "$intent_code" "policy: .after("; then
  sok "the timeline honours the registry's pause and cadence"
else
  sfail "$intent_file no longer derives its timeline policy from the surface settings"
fi

# 9. Every metric set the picker can produce must be exercised by a fixture. That
#    check runs in the HARNESS: it decodes each fixture with the REAL wire model
#    and asks whether the set produced any series at all. A filename pattern here
#    would pass on a payload that carries nothing of the set.

echo ""
if [ "$struct_fail" -eq 0 ]; then
  echo "structural leg: all checks passed"
else
  echo "structural leg: FAILED"
fi

rm -rf -- "$TMP"
exit $(( harness_rc + struct_fail ))
