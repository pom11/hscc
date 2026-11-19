#!/bin/bash
# cluster_widget_check.sh — prove what the cluster widget shows, by EXECUTING
# the committed projection + text model.
#
# The surface mirrors `hscc cluster monitor` / `hscc cluster status`:
#   * the headline is the CLI host tally (answered / listed), never a state word
#     the host table does not print;
#   * every node row is that host's sample + its serving-table model;
#   * the throughput is the CLI total row's sum of serving[].gen_tps;
#   * the temperatures are the sample's CPU/GPU temps, not a load average the
#     CLI host table does not show;
#   * the old hard-coded `.244/.246/.247/.248` topology glyph is gone — four
#     invented node identities were the biggest "does not match the CLI" bug;
#   * the kanban board row is gone from the cluster tile; it is not cluster
#     monitor output (the board widgets own that data);
#   * the three Lock Screen accessories draw the four numbers the CLI's rows add
#     up to — hottest temperature, GPU load (a MEAN over the hosts that
#     reported), fleet watts (a SUM) and running/waiting — projected from the
#     same view-model by Sources/Shared/ClusterLockScreen.swift, so a Lock
#     Screen temperature is the CLI's own cell and not a second rounding.
#
# Reads are named methods on WidgetDataClient, so the closed GET allowlist stays
# the gate. Structural greps pin the family dispatch and the removal of the
# invented glyph; the macOS harness executes the real source against the
# committed, scrubbed live capture.
#
# Usage: ios-app/scripts/cluster_widget_check.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
SDK=$(xcrun --sdk macosx --show-sdk-path 2>/dev/null) || {
  echo "error: no macOS SDK (is Xcode installed?) " >&2; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

SUBJECT=Sources/HSCCWidgets/ClusterWidget.swift
VIEWS=Sources/HSCCWidgets/ClusterWidgetViews.swift
TABLE_VIEWS=Sources/HSCCWidgets/ClusterMonitorTableViews.swift
METRICS=Sources/HSCCWidgets/ClusterWidgetMetrics.swift
READING=Sources/HSCCWidgets/CLIMonitorTableReading.swift
TABLE=Sources/HSCCWidgets/CLIMonitorHostTableView.swift
MONVIEWS=Sources/HSCCWidgets/MonitorViews.swift
MODEL=Sources/Shared/ClusterMonitorViewModel.swift
TALLY=Sources/Shared/ClusterNodeTally.swift
# The systemSmall tile's data half (t_9e110d88): `ClusterWidgetMetrics.swift`
# (the file this harness compiles) now carries the tile the small family draws, so
# the harness must see that type too. It is Foundation-only, like everything else
# compiled here; the view-model it reads is already `$MODEL`.
GRID=Sources/HSCCWidgets/ClusterSmallGrid.swift
# The history series model (t_2681d3de): `$GRID` now builds its GPU% trends
# from ClusterHistorySeriesSet + the pixel-column downsampler, so the harness
# that compiles the tile must compile the series too (both Foundation-only).
HIST=Sources/Shared/ClusterHistorySeries.swift
STORE=Sources/Shared/SharedStore.swift
# The Lock Screen's four numbers + the one copy rule its three families share
# (t_314a3811). Foundation-only like $MODEL above, which is what lets the
# harness execute them: every number here is projected from that view-model.
LOCK=Sources/Shared/ClusterLockScreen.swift
# The alert-feed wire types (R3, t_c6f4a807): `$STORE`'s `WidgetDataClient` now
# declares `alerts(limit:)` returning `AlertsLite`, so the harness compiling the
# client must compile the file that declares it too. Foundation-only.
NEEDS=Sources/Shared/NeedsMeReading.swift
for f in "$SUBJECT" "$VIEWS" "$TABLE_VIEWS" "$METRICS" "$READING" "$TABLE" "$MONVIEWS" \
         "$MODEL" "$TALLY" "$GRID" "$HIST" "$STORE" "$LOCK" "$NEEDS" \
         Sources/Shared/ClusterMonitorTables.swift \
         scripts/cluster_widget_check/main.swift; do
  [ -f "$f" ] || { echo "error: $f missing" >&2; exit 1; }
done

rc=0
report() {  # report <label> <grep-output> — non-empty output is a failure
  if [ -n "${2:-}" ]; then
    echo "FAIL: $1"; echo "$2" | sed 's/^/    /'; rc=1
  else
    echo "  ok: $1"
  fi
}

fail_when_absent() {  # fail_when_absent <label> <file> <pattern>
  if grep -q -- "$3" "$2" 2>/dev/null; then
    echo "  ok: $1"
  else
    echo "FAIL: $1  (no '$3' in $2)"; rc=1
  fi
}

echo "structural proofs on the cluster widget file set"

report "no /v1/ route literal in the widget file set (reads go through the client)" \
  "$(grep -n '"/v1/' "$SUBJECT" "$VIEWS" "$METRICS")"

reads=$(grep -c 'WidgetData.read(' "$SUBJECT")
report "3 WidgetData.read sites (monitor + gated status + gated history), got $reads" \
  "$([ "$reads" -eq 3 ] || echo x)"
fail_when_absent "the row read is cluster/monitor" "$SUBJECT" '$0.clusterMonitor()'
fail_when_absent "the job/host read is cluster/status" "$SUBJECT" '$0.clusterStatus()'
fail_when_absent "the series read is cluster/history" "$SUBJECT" '$0.clusterHistory(window:'
# t_2681d3de: §2's budget is per SURFACE PER REFRESH, and the file holds it BY
# CONSTRUCTION — status and history each sit behind a family gate whose
# disabled arm returns without sending, and the family switches decide which
# size pays for which. Assert both halves: the gates exist, and the switches
# answer them so no family holds more than two reads (monitor + one of the
# pair). A gate count of 3 sites with no `enabled:` arms WOULD be three
# requests per refresh, and that is what these checks refuse.
fail_when_absent "the status read is family-gated" "$SUBJECT" \
  'Self.statusRead(client: client, enabled: family.showsStatus)'
fail_when_absent "the history read is family-gated" "$SUBJECT" \
  'Self.historyRead(client: client, enabled: family.wantsHostHistory)'
skips=$(grep -c 'guard enabled else { return nil }' "$SUBJECT")
report "both gated reads skip the request entirely when disabled, got $skips (want 2)" \
  "$([ "$skips" -eq 2 ] || echo x)"
fail_when_absent "the series read uses the tile's own window" "$SUBJECT" \
  'ClusterSmallGrid.trendWindow'
fail_when_absent "the series read uses the tile's own metric" "$SUBJECT" \
  'ClusterSmallGrid.trendMetric.rawValue'
fail_when_absent "the small tile joins its entry's series into the grid" "$METRICS" \
  'ClusterSmallGrid.tile(from: monitor, history: history)'
# The switches themselves (the one WidgetFamily extension, in MonitorViews).
# Extract each var's own body so the claim cannot be satisfied by an
# unrelated arm elsewhere in the file.
fam_slice() {  # fam_slice <var> — the body of one WidgetFamily computed var
  awk -v v="var $1: Bool {" 'index($0, v) { f = 1 } f { print } f && /^    \}$/ { exit }' "$MONVIEWS"
}
if fam_slice wantsHostHistory | grep -A1 'case \.systemSmall' | grep -q 'return true'; then
  echo "  ok: systemSmall pays for the history read"
else
  echo "FAIL: systemSmall no longer pays for the history read — the tile has no series"; rc=1
fi
if fam_slice showsStatus | grep -A1 'case \.systemSmall' | grep -q 'return false'; then
  echo "  ok: systemSmall trades status for history (two reads, never three)"
else
  echo "FAIL: systemSmall still pays for cluster/status alongside history — 3 reads on one surface"; rc=1
fi
# And the monitor widget's pair: small may not pay for daemon-host alongside
# history either (its slot went to the series, same swap, same budget).
dd_slice=$(fam_slice showsDaemonHost)
if printf '%s\n' "$dd_slice" | grep -A1 '\.systemSmall' | grep -q 'return true'; then
  echo "FAIL: systemSmall still pays for daemon-host alongside history — 3 reads on one surface"; rc=1
elif printf '%s\n' "$dd_slice" | grep -q '\.systemSmall'; then
  echo "  ok: systemSmall is excluded from the daemon-host read (listed false)"
elif printf '%s\n' "$dd_slice" | grep -A1 'default:' | grep -q 'return false'; then
  echo "  ok: systemSmall is excluded from the daemon-host read (falls to the false default)"
else
  echo "FAIL: showsDaemonHost does not refuse systemSmall by a readable arm"; rc=1
fi
report "the cluster tile no longer reads autodown or kanban" \
  "$(grep -n 'autodownStatus\|kanbanRunning\|kanbanBlocked\|kanbanStale' "$SUBJECT")"

report "the invented .244/.246/.247/.248 topology is gone" \
  "$(grep -n '\.244\|\.246\|\.247\|\.248\|canonicalPairs\|TopologyPair' "$SUBJECT" "$VIEWS" "$METRICS")"

# Was: "the load average the CLI host table does not print is gone", over the
# whole file set. t_9e64d9d9 gave extra-large a per-host detail block that
# prints the route's load 1/5/15 — a field `cluster_live.py` never prints, but
# one the route sends and the surface labels for what it is, on the single size
# with room for it. So the ban keeps its teeth exactly where the tile mirrors
# the CLI's own output, and points at the file that owns the XL-only fields
# instead of pretending they do not exist.
report "no load average anywhere the tile mirrors \`cluster monitor\` output" \
  "$(grep -n 'cpuLoad1m\|load1m' "$SUBJECT" "$VIEWS" "$METRICS")"
fail_when_absent "the extra-large-only fields are selected by the shared layout" \
  "$TABLE_VIEWS" 'ClusterMonitorTableLayout.visibleDetails'
fail_when_absent "…and not formatted by the view" \
  "$TABLE_VIEWS" 'CLIMonitorHostRow'

for fam in systemSmall systemMedium systemLarge systemExtraLarge \
           accessoryInline accessoryCircular accessoryRectangular; do
  fail_when_absent "supportedFamilies offers .$fam" "$VIEWS" ".$fam"
done
for fam in systemSmall systemMedium systemLarge systemExtraLarge \
           accessoryInline accessoryCircular accessoryRectangular; do
  fail_when_absent "the view dispatches .$fam" "$VIEWS" "case .$fam:"
done
fail_when_absent "large has its own layout" "$VIEWS" 'struct LargeClusterWidget'
fail_when_absent "extra-large has its own layout, not a re-used large one" \
  "$VIEWS" 'struct ExtraLargeClusterWidget'
fail_when_absent "the two biggest sizes draw the shared CLI tables" \
  "$VIEWS" 'MonitorTablesBlock(tables:'
fail_when_absent "…and ask the entry whether a table may be drawn at all" \
  "$VIEWS" 'if let tables = entry.tables'
fail_when_absent "the entry owns that gate" "$METRICS" 'var tables: ClusterMonitorTables?'

fail_when_absent "every family renders the shared text model" "$VIEWS" 'entry.text(for:'
# The small family is the DENSE GRID now (t_9e110d88): up to four hosts x four CLI
# columns plus the serving totals, drawn by ClusterSmallGridView. The old claim
# here was "small is one headline + detail" (a 32pt number), which is exactly the
# shape this card was written to end, so the anchor moved to the grid type. Two
# arms stay asserted: the grid is reached through the entry's ONE gate step (so no
# branch can grid dashes over a paused surface), and the headline the tile shows
# beside it still comes from the shared text model rather than a re-composed
# number. The size is deliberately not pinned — a font size is not a claim about
# honesty, and pinning one is what made this anchor rot.
fail_when_absent "small draws the dense grid" "$VIEWS" 'ClusterSmallGridView(tile:'
fail_when_absent "small reaches the grid through the entry's gate" "$VIEWS" \
  'if let tile = entry.smallTile'
fail_when_absent "small's headline still comes from the shared text model" "$VIEWS" \
  'ClusterHeadline(entry: entry, size:'
fail_when_absent "circular uses the up/total gauge" "$VIEWS" 'Gauge(value:'

fail_when_absent "the projection uses the shared FleetProjection" "$METRICS" 'typealias ClusterFleet = FleetProjection'
fail_when_absent "status jobs come from workloads.count" "$METRICS" 'response.workloads.count'

# ---- The serving-block note line (t_b5d0ec18).
#
# ClusterMonitorTables projected proxyError and NO iOS view drew it: the phone
# met the desktop's proxy-unreachable failure and said nothing while the menu bar
# printed the terminal's line. The sentence, its precedence and its warn/dim rule
# now live in ONE type -- CLIMonitorNote, in $MODEL, which all five iOS targets
# and both macOS targets compile -- and $TABLE_VIEWS draws it.
#
# The leg with teeth is the last one. "the note appears" cannot rot into a lie on
# its own, because a view could print its own re-worded sentence and still
# satisfy an appearance grep; banning a string literal of the terminal's words
# anywhere outside the owner is what keeps the appearance honest. The words
# themselves are pinned by execution in two other gates -- model_decode_check/
# monitor_tables.swift (against the CLI's own rendered golden) and macos-app/
# scripts/cli_dropdown_check/main.swift (the dropdown's Tile.note).
fail_when_absent "the note owner is declared in the shared view-model file" \
  "$MODEL" 'enum CLIMonitorNote'
fail_when_absent "both projections answer from that one owner" "$MODEL" \
  'CLIMonitorNote.servingBlock(proxyError:'
fail_when_absent "the serving column draws the owner line, not its own words" \
  "$TABLE_VIEWS" 'MonitorTableNote(text: note.text'
report "no iOS file outside the owner re-types the CLI note words" \
  "$(grep -rn '"proxy /v1/model/info\|"no backends behind the proxy\|"serving poll failed' \
        Sources --exclude=ClusterMonitorViewModel.swift)"

# ---------------------------------------------------------------------------
# The CLI host table (t_2d1c12dc): `systemMedium` IS `host_table`.
#
# The executed half below proves the cells, the titles, the verdicts and the
# row budget. These structural proofs cover what an executed check cannot see:
# that the two CLI-shaped tiles share the ONE table view, and that no tile has
# quietly re-typed a column name (a transcription is a column that can drift
# from the CLI while every executed assertion still passes).
# ---------------------------------------------------------------------------
echo ""
echo "CLI host-table proofs (the medium tile draws host_table)"

fail_when_absent "the cluster medium draws the shared CLI table" "$VIEWS" 'CLIMonitorHostTableView(rows:'
fail_when_absent "the monitor medium draws the SAME table" "$MONVIEWS" 'CLIMonitorHostTableView(rows:'
fail_when_absent "both entries hand it rows through the gate-checked accessor" "$VIEWS" 'entry.cliHostRows'
fail_when_absent "and so does the monitor entry" "$MONVIEWS" 'entry.cliHostRows'
fail_when_absent "the drawn titles are the CLI's own column list" "$READING" 'CLIMonitorHostRow.columns'
fail_when_absent "the drawn cells are the CLI's own row" "$READING" 'row.cells'
fail_when_absent "the verdicts come from the view-model" "$READING" 'ClusterMetricLevel'

# No CLI title re-typed into a body. A grep for the CLI's own header words in
# the view/metrics files means a second copy of the column list exists, and a
# second copy is a column that drifts.
report "no CLI column title is transcribed into a widget body" \
  "$(grep -n '"CPU Temp"\|"GPU Temp"\|"GPU Power"\|"Swap%"' "$VIEWS" "$MONVIEWS" "$METRICS")"

# A family that reaches `entry.monitor` directly is a family that skipped
# `cliHostRows`, which is the one place unconfigured / paused / "nodes" off are
# refused. Numbers on a paused surface is the t_5c554c5b failure again.
report "no family reaches entry.monitor directly (rows go through cliHostRows)" \
  "$(grep -n 'entry\.monitor' "$VIEWS" "$MONVIEWS")"

# The reading layer stays Foundation-only so the checks below can compile and RUN
# it. The comment lines are excluded — the file's own header explains why it must
# not name a Colour, and prose is not a colour (same rule the paused-copy count
# learned on #341).
report "the reading layer names no SwiftUI and no Colour, so it is executed below" \
  "$(grep -n 'import SwiftUI\|Color' "$READING" | grep -v '^[0-9]*:[[:space:]]*//')"

# The tile draws cells; it does not write any. Every `Text` in it takes a value
# from the reading layer or the view-model.
report "the table view composes no cell text of its own" \
  "$(grep -n 'Text(\s*"' "$TABLE")"

# §7: the workload projection may name ids only — no recipe path, image or
# container name may even be decoded.
wl=$(sed -n '/^struct MonitorWorkload/,/^}/p' Sources/Shared/SharedModels.swift)
report "MonitorWorkload decodes only ids (no recipe/containers/image)" \
  "$(printf '%s\n' "$wl" | grep -n 'recipe\|containers\|image')"
for key in workload_id cluster_id; do
  report "$key is declared without a default (a default would skip decoding)" \
    "$(printf '%s\n' "$wl" | grep -n "let $key:.*= nil")"
done

echo ""
echo "executed proofs (macOS compile of the real widget + shared sources)"

xcrun swiftc -sdk "$SDK" -o "$TMP/cluster_widget_check" \
  "$METRICS" \
  "$TALLY" \
  "$MODEL" \
  "$READING" \
  "$GRID" \
  Sources/Shared/ClusterMonitorTables.swift \
  "$HIST" \
  "$LOCK" \
  "$NEEDS" \
  Sources/Shared/SharedModels.swift Sources/Shared/SharedStore.swift \
  Sources/Shared/WidgetDataClient.swift \
  scripts/model_decode_check/ThemeStub.swift \
  scripts/cluster_widget_check/main.swift \
  2> "$TMP/compile.err"
if [ -s "$TMP/compile.err" ]; then
  echo "FAIL: harness compile produced diagnostics:"
  sed 's/^/    /' "$TMP/compile.err"
  exit 1
fi
"$TMP/cluster_widget_check" || rc=1

echo ""
if [ "$rc" -eq 0 ]; then
  echo "CLUSTER WIDGET CHECK PASSED — CLI rows, every family's lines, nil-is-not-zero, redaction"
  exit 0
fi
exit 1
