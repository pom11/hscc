import AppKit
import SwiftUI

// ===========================================================================
// The dropdown's two CLI surfaces: `hscc cluster monitor`'s host + serving
// tables, and `hscc cluster status`'s workloads, hosts and slots.
//
// This file draws strings. It owns layout, alignment and which of the
// view-model's threshold verdicts paints a cell — nothing else. Every number,
// unit, rounding and dash comes from `DashboardFleetReading`, which is itself
// only a re-exposure of `ClusterMonitorViewModel`'s CLI-parity cells (the
// parent card's `scripts/cluster_monitor_cli_parity.py` pins those against the
// CLI's own `host_table`/`serving_table`). `macos-app/scripts/cli_dropdown_check.sh`
// holds the rule this split depends on: no number formatting in this file.
//
// Colour is the view-model's verdict painted through the one `Theme` the iOS
// app, the widgets and the rest of this dropdown already use — and, exactly as
// `host_table` does, an `ok` cell stays in primary ink. The monitor CLI colours
// exactly one thing (the serving `Run` column, via `_load_style`), so a wall of
// green numbers would stop looking like the CLI.
// ===========================================================================

/// The serving table's column widths. The pane is 360 pt wide minus its own
/// `Theme.Spacing.lg` padding, so about 328 pt — roughly 55 characters at
/// `caption2`. `serving_table`'s NARROW row (the shape the CLI builds below
/// `WIDE_MIN`) is about 72 characters wide, so this pane is narrower than even
/// the CLI's narrow form: `Gen t/s` is the column that drops, and every column
/// that stays is wide enough for the widest CLI cell it can hold (`100.0%` in
/// KV%) so no CLI string is ever ellipsised into a different number. Model
/// names are the one exception, and they ellipsis rather than shift.
private enum ServingWidth {
    static let node: CGFloat = 30
    static let port: CGFloat = 34
    static let value: CGFloat = 36
    static let gap: CGFloat = 3
}

extension DashboardView {
    /// `hscc cluster monitor`: the host table (GPU%, GPU Temp, CPU Temp, GPU
    /// Power) then the serving table (Run, Wait, KV%, In, Out) with the CLI's
    /// per-model `total (n)` rows.
    var clusterMonitorSection: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs.rawValue) {
            sectionTitle("GPU")
            if let tile = dashboard.snapshot.fleetTile {
                HStack(spacing: Theme.Spacing.sm.rawValue) {
                    Text(DashboardFleetReading.Copy.hosts(tile.hostCount))
                        .font(.caption2.monospacedDigit())
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    Spacer()
                    queueLine(tile.totals)
                }
                hostHeader
                ForEach(tile.rows) { row in
                    hostRow(row)
                }
                if tile.hiddenHosts > 0 {
                    moreLine(tile.hiddenHosts, "host")
                }
                // `hscc cluster monitor`'s `node pauses` block: one line per
                // parked machine, carrying the full verdict string the CLI puts
                // under the table when its Model column is too narrow for it.
                // This is also the ONLY place a parked node the frame never
                // listed can appear — no host row exists for it to draw.
                if !tile.parkedLines.isEmpty {
                    parkedBlock(tile.parkedLines)
                }

                Divider().overlay(Theme.Semantic.surfaceElevated)
                servingHeader
                servingGridHeader
                ForEach(tile.serving) { row in
                    servingRow(row)
                }
                if tile.hiddenServingRows > 0 {
                    moreLine(tile.hiddenServingRows, "serving row")
                }
                if let note = tile.note {
                    Text(note)
                        .font(.caption2)
                        .foregroundColor(Theme.Semantic.warn)
                        .fixedSize(horizontal: false, vertical: true)
                }
            } else {
                Text(dashboard.snapshot.fleetEmptyText ?? "—")
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    /// `hscc cluster status`: the workload table, the `cluster summary` host
    /// tally, and the per-node slot pair.
    var clusterStatusSection: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs.rawValue) {
            sectionTitle("Cluster status")
            if let panel = dashboard.snapshot.statusPanel {
                if panel.workloads.isEmpty {
                    Text(ClusterStatusReading.Copy.noWorkloads)
                        .font(.caption)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                } else {
                    ForEach(panel.workloads) { workload in
                        workloadRow(workload)
                    }
                    if panel.hiddenWorkloads > 0 {
                        moreLine(panel.hiddenWorkloads, "workload")
                    }
                }
                Text(panel.hostsLine)
                    .font(.caption.monospacedDigit())
                    .foregroundColor(Theme.Semantic.onSurface)
                // The slot pair, labelled as coming from the monitor route:
                // `/v1/cluster/status` has no per-node slots to give.
                ForEach(panel.slots) { slot in
                    HStack(spacing: Theme.Spacing.sm.rawValue) {
                        Text(slot.label)
                            .font(.caption2)
                            .foregroundColor(Theme.Semantic.onSurface)
                        Spacer()
                        Text("\(slot.used) used / \(slot.free) free")
                            .font(.caption2.monospacedDigit())
                            .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    }
                    .accessibilityLabel("\(slot.label), \(slot.used) used, \(slot.free) free slots")
                }
                if panel.hiddenSlots > 0 {
                    moreLine(panel.hiddenSlots, "host slot line")
                }
            } else if let error = dashboard.snapshot.statusError {
                // A degraded route answers `total_hosts: 0`; printing the tally
                // would read as an empty cluster rather than a failed read.
                Text(error)
                    .font(.caption2)
                    .foregroundColor(Theme.Semantic.warn)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                Text("—")
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(dashboard.snapshot.statusPanel?.accessibilityLabel
                            ?? dashboard.snapshot.statusError
                            ?? "No cluster status data")
    }

    // MARK: - Host table pieces

    /// The four column titles, straight out of `host_table`'s own column list.
    private var hostHeader: some View {
        HStack(spacing: Theme.Spacing.sm.rawValue) {
            Text(ClusterSmallGrid.hostColumnLabel)
                .frame(width: 46, alignment: .leading)
            ForEach(ClusterSmallGrid.Column.allCases) { column in
                Text(column.cliColumn)
                    .frame(maxWidth: .infinity, alignment: .trailing)
            }
        }
        .font(.caption2.weight(.semibold))
        .foregroundColor(Theme.Semantic.onSurfaceMuted)
        .accessibilityHidden(true)
    }

    private func hostRow(_ row: DashboardHostRow) -> some View {
        HStack(spacing: Theme.Spacing.sm.rawValue) {
            Text(row.label)
                .frame(width: 46, alignment: .leading)
                .foregroundColor(Theme.Semantic.onSurface)
            ForEach(row.cells) { cell in
                Text(cell.text)
                    .frame(maxWidth: .infinity, alignment: .trailing)
                    .foregroundColor(cellColor(cell.level))
            }
        }
        .font(.caption2.monospacedDigit())
        .accessibilityLabel(row.accessibilityLabel)
    }

    // MARK: - Serving table pieces

    /// `render`'s serving header line, minus the proxy port: the route never
    /// sends it, and this app does not print a number it did not read.
    private var servingHeader: some View {
        HStack(spacing: Theme.Spacing.sm.rawValue) {
            Text("serving")
                .font(.caption2.weight(.semibold))
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
            Spacer()
            Text(DashboardFleetReading.Copy.tokensNote)
                .font(.caption2)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
        }
        .accessibilityHidden(true)
    }

    /// `serving_table`'s narrow-row column titles, in its order.
    private var servingGridHeader: some View {
        HStack(spacing: ServingWidth.gap) {
            Text("Node").frame(width: ServingWidth.node, alignment: .leading)
            Text("Port").frame(width: ServingWidth.port, alignment: .trailing)
            Text("Model").frame(maxWidth: .infinity, alignment: .leading)
            ForEach(DashboardServingRow.Column.allCases) { column in
                Text(column.cliColumn)
                    .frame(width: ServingWidth.value, alignment: .trailing)
            }
        }
        .font(.caption2.weight(.semibold))
        .foregroundColor(Theme.Semantic.onSurfaceMuted)
        .accessibilityHidden(true)
    }

    private func servingRow(_ row: DashboardServingRow) -> some View {
        HStack(spacing: ServingWidth.gap) {
            Text(row.nodeLabel)
                .frame(width: ServingWidth.node, alignment: .leading)
                .foregroundColor(row.isTotalRow ? Theme.Semantic.accent : Theme.Semantic.onSurface)
            Text(row.port)
                .frame(width: ServingWidth.port, alignment: .trailing)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
            // The Model cell, which is where `serving_table` puts the verdict for
            // a parked backend (`warn`, never `down` in `error`). Like every
            // string in this column it can outgrow the space — the CLI ellipsises
            // the same cell at its 26-char cap — and the `node pauses` block above
            // is where the whole verdict is readable, in both places.
            Text(row.model)
                .frame(maxWidth: .infinity, alignment: .leading)
                .foregroundColor(row.isTotalRow ? Theme.Semantic.accent
                                    : row.isPaused ? Theme.Semantic.warn
                                    : Theme.Semantic.onSurface)
                .lineLimit(1)
                .truncationMode(.middle)
            ForEach(DashboardServingRow.Column.allCases) { column in
                Text(row.cells[column] ?? CLIMonitorFormat.dash)
                    .frame(width: ServingWidth.value, alignment: .trailing)
                    .foregroundColor(column == .run ? queueColor(row.level) : Theme.Semantic.onSurface)
            }
        }
        .font(.caption2.monospacedDigit())
        .accessibilityLabel(row.accessibilityLabel)
    }

    /// The run/wait glance. `countText` keeps a not-told section at `-`, so the
    /// line can never read `0 running` for a route that said nothing (§6 rule 1).
    private func queueLine(_ totals: ClusterSmallGrid.ServingTotals) -> some View {
        Text("run \(ClusterSmallGrid.countText(totals.running)) · "
             + "wait \(ClusterSmallGrid.countText(totals.waiting))")
            .font(.caption2.monospacedDigit())
            .foregroundColor(queueColor(totals.level))
    }

    // MARK: - Shared pieces

    private func workloadRow(_ workload: DashboardWorkloadRow) -> some View {
        HStack(spacing: Theme.Spacing.sm.rawValue) {
            Text(workload.name)
                .font(.caption2)
                .foregroundColor(Theme.Semantic.onSurface)
                .lineLimit(1)
                .truncationMode(.middle)
            Spacer(minLength: Theme.Spacing.xs.rawValue)
            // The CLI's `tp` / `pp` columns, verbatim.
            Text("tp \(workload.tp)")
                .font(.caption2.monospacedDigit())
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
            Text("pp \(workload.pp)")
                .font(.caption2.monospacedDigit())
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
        }
        .accessibilityLabel("workload \(workload.name), tp \(workload.tp), pp \(workload.pp)")
    }

    /// `render`'s `node pauses` block: the title line, then one row per parked
    /// machine. Every string here was built by `DashboardFleetReading` from
    /// `FleetProjection.paused` + `FleetPausedNode.statusText`; this view adds no
    /// word and no number of its own, and it is `warn` for the reason
    /// `cluster_render.pause_lines` gives — a machine off the fleet by deliberate
    /// choice is not red (a fault) and not dim (nobody should have to look twice).
    private func parkedBlock(_ lines: [String]) -> some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs.rawValue) {
            Text(DashboardFleetReading.Copy.nodePauses)
                .font(.caption2.weight(.semibold))
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
            ForEach(Array(lines.enumerated()), id: \.offset) { _, line in
                Text(line)
                    .font(.caption2)
                    .foregroundColor(Theme.Semantic.warn)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(DashboardFleetReading.Copy.nodePauses): \(lines.joined(separator: ", "))")
    }

    private func moreLine(_ hidden: Int, _ noun: String) -> some View {
        Text("\(hidden) more \(noun)\(hidden == 1 ? "" : "s") not shown")
            .font(.caption2)
            .foregroundColor(Theme.Semantic.onSurfaceMuted)
    }

    // MARK: - Colour

    /// The view-model's verdict, painted. `ok` and `dim` are plain ink: the CLI
    /// colours neither temperature nor wattage anywhere.
    private func cellColor(_ level: ClusterMetricLevel) -> Color {
        switch level {
        case .critical: return Theme.Semantic.bad
        case .warn: return Theme.Semantic.warn
        case .ok: return Theme.Semantic.onSurface
        case .dim, .unknown: return Theme.Semantic.onSurfaceMuted
        }
    }

    /// `_load_style`, the CLI's only threshold: a queue is warn, a busy engine
    /// is ok, an idle one dim.
    private func queueColor(_ level: ClusterMetricLevel) -> Color {
        switch level {
        case .warn: return Theme.Semantic.warn
        case .ok: return Theme.Semantic.onSurface
        case .dim, .unknown: return Theme.Semantic.onSurfaceMuted
        case .critical: return Theme.Semantic.bad
        }
    }
}
