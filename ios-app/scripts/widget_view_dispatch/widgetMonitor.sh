#!/bin/bash
# widget_view_dispatch/widgetMonitor.sh — the HSCC MONITOR widget surface
# (WidgetSurfaceKind.widgetMonitor).
#
# Subject: Sources/HSCCWidgets/MonitorWidget.swift (provider/model) and
# Sources/HSCCWidgets/MonitorViews.swift (families). This surface predates the
# shared `widget_surface_gate` because its reads go through the older
# `ExtensionClient`/`APIConfig` pair rather than `WidgetDataClient`, so the
# checks below are explicit rather than a gate call.
#
# What this file proves:
#   * the widget offers small, medium and large plus all three Lock Screen
#     families, and the dispatcher names every one of them;
#   * every family body takes the unconfigured/paused gate in ONE step, and the
#     accessory copy routes through the entry's `shortGateLabel`, which checks
#     `!configured` before it can say "Paused";
#   * the new compact families draw only numbers the medium view already has
#     (first node's CPU/RAM/GPU, fleet CPU) — no new route and no second fetch;
#   * the accessory refresh refuses the daemon-host request BEFORE it is sent
#     (`showsDaemonHost`, the same positive-list decision `ClusterWidget` uses
#     for its board row), so a Lock Screen family never pays for a block it
#     cannot draw;
#   * the ring and the row draw `hscc cluster monitor`'s four Lock Screen
#     numbers — hottest temperature, GPU load, watts, running/waiting — through
#     the shared copy rule, from the SAME monitor read as their fleet rows, and
#     derive or format no number themselves;
#   * a family that does not fetch the host leaves the stored host half alone
#     instead of erasing it from the last-known snapshot.
#
# Run standalone, or through the aggregator:
#   ./ios-app/scripts/widget_view_dispatch/widgetMonitor.sh
set -uo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

SUBJECT=Sources/HSCCWidgets/MonitorWidget.swift
VIEWS=Sources/HSCCWidgets/MonitorViews.swift
[ -f "$SUBJECT" ] || { wbad "missing $SUBJECT"; wvd_finish; exit $?; }
[ -f "$VIEWS" ] || { wbad "missing $VIEWS"; wvd_finish; exit $?; }

# (1) The family list. Small and all three accessories are the card; medium and
# large must not be lost in the same edit. Measured inside the supportedFamilies
# block, not file-wide: a dispatch case proves the family is handled, not that
# WidgetKit is allowed to offer it.
family_list=$(sed -n '/\.supportedFamilies(\[/,/)\]/p' "$VIEWS")
for fam in systemSmall systemMedium systemLarge systemExtraLarge \
           accessoryInline accessoryCircular accessoryRectangular; do
  if printf '%s\n' "$family_list" | grep -q "\.$fam"; then
    wok "supportedFamilies offers .$fam"
  else
    wbad "supportedFamilies does not offer .$fam"
  fi
done

# (1b) t_9e64d9d9: the two biggest surfaces draw the CLI's own tables, and the
# decision "is there a table to draw at all" stays owned by the entry — the same
# one-step-gate invariant this directory exists to enforce. A body that decided
# it locally could paint a live-looking table on a paused or unconfigured tile.
tables_blocks=$(grep -c 'MonitorTablesBlock(tables:' "$VIEWS")
echo "monitor table blocks = $tables_blocks (want >= 2: large + extra-large)"
[ "$tables_blocks" -ge 2 ] || wbad "a monitor surface stopped drawing the shared CLI tables"
grep -q 'if let tables = entry.tables' "$VIEWS" \
  || wbad "a monitor body decides the table gate itself instead of asking the entry"
grep -q 'var tables: ClusterMonitorTables?' "$SUBJECT" \
  || wbad "MonitorEntry no longer owns the table gate"
grep -q 'struct ExtraLargeMonitorWidget' "$VIEWS" \
  || wbad "extra-large has no layout of its own, so the XL-only fields are lost"

# (2) Registration without dispatch is a family that never renders.
for fam in systemSmall accessoryInline accessoryCircular accessoryRectangular; do
  if grep -q "case \.$fam:" "$VIEWS"; then
    wok "the view dispatches .$fam"
  else
    wbad "the view has no case for .$fam"
  fi
done

# (3) One gate owner, not six copies: every family routes through
# `MonitorEntry.text(for:)`, and that entry checks !configured before paused.
n=$(grep -c 'entry.text(for:' "$VIEWS")
if [ "$n" -ge 6 ]; then
  wok "$n family sites route through the entry's gated text model (want >= 6)"
else
  wbad "only $n family sites route through entry.text(for:) (want >= 6) — a family can draw data on a paused surface"
fi

# (4) The paused copy has exactly one CODE owner (MonitorState.label). The gate
# pane reads that label instead of spelling the copy again.
n=$(grep '"Paused in Settings"' "$SUBJECT" "$VIEWS" | grep -v '^[^:]*:[[:space:]]*//' | wc -l | tr -d ' ')
if [ "$n" -eq 1 ]; then
  wok "one paused copy, owned by MonitorState.label"
else
  wbad "\"Paused in Settings\" appears $n times (want exactly 1 — the copy must be owned by the entry state)"
fi

# (5) The accessory label rule, and the invite-first order inside it.
if grep -q 'var shortGateLabel: String' "$SUBJECT"; then
  wok "the entry owns the accessory shortGateLabel"
else
  wbad "no shortGateLabel on the entry — a Lock Screen gate has no copy of its own"
fi
copy=$(awk '/var shortGateLabel: String \{/ {f=1} f {print} f && /^    \}$/ {exit}' "$SUBJECT")
if [ -z "$copy" ]; then
  wbad "could not locate var shortGateLabel in $SUBJECT — the invite-first rule is not being measured"
else
  u=$(printf '%s\n' "$copy" | grep -n '!configured' | head -1 | cut -d: -f1)
  p=$(printf '%s\n' "$copy" | grep -n 'Paused' | head -1 | cut -d: -f1)
  if [ -n "$u" ] && [ -n "$p" ] && [ "$u" -lt "$p" ]; then
    wok "shortGateLabel checks !configured (line $u) before any paused copy (line $p) — an unconfigured device cannot read as paused"
  else
    wbad "shortGateLabel does NOT check !configured first (configured-check line=$u paused-copy line=$p)"
  fi
fi
n=$(grep -cE 'entry\.text\(for: \.(inline|circular|rectangular)\)' "$VIEWS")
if [ "$n" -ge 2 ]; then
  wok "accessory families route through the entry's gated text ($n sites)"
else
  wbad "only $n accessory sites route through the gate (want >= 2 — inline and circular)"
fi

# (6) The compact families draw a subset of the same entry text, not a new
# route and not an invented average: every family renders `text(for:)`, whose
# headline is the host tally and whose rows are the CLI node rows.
for helper in 'func text(for surface: FleetSurface)' 'var inlineLine' 'func daemonLine'; do
  if grep -q "$helper" "$SUBJECT"; then
    wok "the entry owns '$helper'"
  else
    wbad "missing '$helper' on the entry — a compact family is inventing its own numbers"
  fi
done
small_range=$(awk '/^struct SmallMonitorWidget/,/^}/' "$VIEWS")
if printf '%s\n' "$small_range" | grep -q 'MonitorHeadline(entry:'; then
  wok "the small family draws the entry's tally + serving line"
else
  wbad "the small family bypasses the entry text model"
fi
accessory_range=$(awk '/^struct InlineMonitorWidget/,0' "$VIEWS")
if printf '%s\n' "$accessory_range" | grep -q 'entry.inlineLine'; then
  wok "the accessory families draw the same gated text"
else
  wbad "an accessory family bypasses the entry text model"
fi
if printf '%s\n' "$accessory_range" | grep -q 'daemonHost\|HostStrip\|HostBlock\|"host"'; then
  wbad "an accessory body reads the daemon-host half it will not draw"
else
  wok "the accessory bodies never read the daemon-host half"
fi

# (6b) The Lock Screen's four numbers (t_314a3811). The ring and the row draw
# them through the shared copy rule — `ClusterLockScreen.swift`, executed by
# `cluster_widget_check.sh` — so the Monitor tile cannot word them differently
# from the Cluster tile, and no accessory formats a number of its own. The old
# up/total gauge is gone from both: a Lock Screen ring that fills to a node
# count says nothing about hot or working.
for family in CircularMonitorWidget RectangularMonitorWidget; do
  body=$(awk "/^struct $family/,/^}/" "$VIEWS")
  if [ -z "$body" ]; then
    wbad "$family not found in $VIEWS"
    continue
  fi
  if printf '%s\n' "$body" | grep -q 'entry.lockScreen'; then
    wok "$family reads the projected Lock Screen numbers"
  else
    wbad "$family does not read the entry's projected Lock Screen numbers"
  fi
  if printf '%s\n' "$body" | grep -q 'entry.text(for: .'; then
    wok "$family draws its strings from the entry text model"
  else
    wbad "$family composes its own strings"
  fi
  if printf '%s\n' "$body" | grep -q 'tally\|String(format:\|Double(min(max'; then
    wbad "$family recomputes or re-formats a number a view has no business deriving"
  else
    wok "$family derives no number and formats no number"
  fi
done
if grep -q 'CLILockScreenMetrics(monitorOutcome.value)' "$SUBJECT"; then
  wok "the four numbers come from the SAME monitor read as the fleet rows"
else
  wbad "the entry's Lock Screen numbers are not projected from the monitor outcome"
fi
if grep -q 'lockScreen: lockScreen' "$SUBJECT"; then
  wok "the live entry carries the projected numbers"
else
  wbad "the provider never hands the projected numbers to the entry"
fi
if grep -q 'let wantsHost = family.showsDaemonHost' "$SUBJECT"; then
  wok "the daemon-host read is refused before it is sent for a Lock Screen family"
else
  wbad "an accessory refresh may still pay for the daemon-host request it cannot draw"
fi
# The refusal is one shared positive list, not a per-widget rule that can drift:
# a `showsDaemonHost` that ever names an accessory family would put a second
# machine's health on the Lock Screen, where only the fleet's numbers belong.
host_list=$(awk '/var showsDaemonHost/,/^    }/' "$VIEWS")
if printf '%s\n' "$host_list" | grep -q 'accessory'; then
  wbad "showsDaemonHost names an accessory family — the host block would reach the Lock Screen"
else
  wok "showsDaemonHost stays a positive list of the system families"
fi

# (7) No new network call: one named monitor method and one named host method,
# both through WidgetDataClient (the closed GET allowlist), in the provider.
n=$(grep -c '\$0.clusterMonitor()' "$SUBJECT")
if [ "$n" -eq 1 ]; then
  wok "exactly one clusterMonitor() read"
else
  wbad "expected exactly 1 clusterMonitor() read, got $n"
fi
n=$(grep -c '\$0.daemonHost()' "$SUBJECT")
if [ "$n" -eq 1 ]; then
  wok "exactly one daemonHost() read"
else
  wbad "expected exactly 1 daemonHost() read, got $n"
fi
if grep -q '"/v1/' "$VIEWS"; then
  wbad "a route literal appears in the view file — reads belong in the provider"
else
  wok "no /v1/ route literal in the view file"
fi

# (8) The family-aware fetch: both entry points pass the family, and the host
# request is refused before it is sent when the family does not draw it.
n=$(grep -c 'fetchEntry(family: context.family)' "$SUBJECT")
if [ "$n" -eq 2 ]; then
  wok "both snapshot and timeline pass context.family into fetchEntry"
else
  wbad "expected snapshot + timeline to pass context.family, got $n site(s)"
fi
if grep -q 'private func fetchEntry(family: WidgetFamily)' "$SUBJECT"; then
  wok "the provider's fetch is family-aware"
else
  wbad "fetchEntry is not family-aware"
fi
if grep -q 'let wantsHost = family.showsDaemonHost' "$SUBJECT"; then
  wok "the daemon-host read is decided by the family"
else
  wbad "the daemon-host read is not decided by the family"
fi
if grep -q 'case \.systemSmall, \.systemMedium, \.systemLarge, \.systemExtraLarge:' "$VIEWS"; then
  wok "showsDaemonHost positive-lists the Home Screen sizes"
else
  wbad "showsDaemonHost is not the positive list the gate can execute"
fi
guard_line=$(grep -n 'guard enabled else' "$SUBJECT" | head -1 | cut -d: -f1)
host_line=$(grep -n '\$0.daemonHost()' "$SUBJECT" | head -1 | cut -d: -f1)
if [ -n "$guard_line" ] && [ -n "$host_line" ] && [ "$guard_line" -lt "$host_line" ]; then
  wok "the host-refusal guard (line $guard_line) precedes the host request (line $host_line)"
else
  wbad "the host-refusal guard does not precede the host request (guard=${guard_line:-none} host=${host_line:-none})"
fi

# (9) Paused is decided BEFORE the client exists, and the paused entry is the
# entry's own factory — no live nodes, no host, no stale snapshot.
region=$(awk '/private func fetchEntry\(family:/ {f=1} f {print} f && /^    \}$/ {exit}' "$SUBJECT")
if [ -z "$region" ]; then
  wbad "could not locate fetchEntry(family:) — the pause-before-client order is not being measured"
else
  a=$(printf '%s\n' "$region" | grep -n 'if let surface, !surface.isActive' | head -1 | cut -d: -f1)
  c=$(printf '%s\n' "$region" | grep -n 'WidgetDataClient.makeIfConfigured()' | head -1 | cut -d: -f1)
  if [ -n "$a" ] && [ -n "$c" ] && [ "$a" -lt "$c" ]; then
    wok "inside the fetch: paused decided (line $a) before the client exists (line $c) — zero requests when paused"
  else
    wbad "inside the fetch, paused is NOT decided before the client is built (active=$a client=$c)"
  fi
fi
if grep -q 'return \.paused()' "$SUBJECT"; then
  wok "the provider returns the entry's own paused factory"
else
  wbad "the provider does not build its paused entry with .paused()"
fi
if grep -q 'setting(for: \.widgetMonitor)' "$SUBJECT"; then
  wok "cadence and pause come from WidgetSurfaceKind.widgetMonitor (one registry, one write path)"
else
  wbad "$SUBJECT reads its cadence somewhere other than WidgetSurfaceKind.widgetMonitor"
fi

# (10) An accessory refresh must not erase the host half a larger family stored.
if grep -q 'MonitorSnapshotStore.load()?.daemonHost' "$SUBJECT"; then
  wok "a no-host family preserves the stored daemon-host half"
else
  wbad "a no-host refresh would overwrite the stored daemon-host half with nil"
fi

wvd_finish
