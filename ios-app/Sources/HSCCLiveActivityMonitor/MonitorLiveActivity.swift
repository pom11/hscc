import SwiftUI
import WidgetKit
import ActivityKit

/// HSCCLiveActivityMonitor — the ActivityKit configuration for the HSCC
/// Monitor Live Activity.
///
/// Hosted by the `HSCCLiveActivityMonitor` app-extension target, beside the
/// running-cards activity — see `HSCCLiveActivityMonitorBundle`, which is this
/// target's `@main`: one extension can host several Live Activity types, each
/// with its own `ActivityConfiguration`. (The wake activity keeps its own
/// `HSCCLiveActivity` target; nothing here undoes that split.)
///
/// This is the live cluster/daemon-host monitor: it renders the data the APP
/// pushes (`MonitorActivityDriver` polls GET /v1/cluster/monitor +
/// GET /v1/daemon/host on the settings cadence and calls update()). Live
/// Activities are PUSH-ONLY — this extension never fetches; it renders
/// whatever `MonitorActivityAttributes.ContentState` the app last pushed, and
/// honestly ages via the pushed staleDate when the app can't push anymore.
///
/// WHAT EACH SURFACE CARRIES (t_1685f884 — the rules are
/// `MonitorSurfaceReading`'s, in `Sources/Shared/MonitorActivityMetrics.swift`,
/// and the cells are `hscc cluster monitor`'s own strings from
/// `ClusterMonitorViewModel`. This file draws; it decides no number, no unit,
/// no rounding and no threshold):
///
///   * Lock Screen — the state header, then the host table: one row per host
///     with GPU%, CPU Temp, GPU Temp and GPU Power as the CLI prints them, plus
///     that host's serving run/wait; then the serving table's aggregate Run/Wait
///     and its per-model `total (n)` rows; then the daemon-host line.
///   * compact — leading the one primary metric (the hottest temperature the
///     fleet reported, else the busiest host's GPU%, else the serving count),
///     trailing the secondary (a queue, the live run count, else the tally).
///   * expanded — leading the state dot, center the headline + the primary
///     metric with the host it belongs to, trailing the tally + secondary,
///     bottom the host table (its own rows are clipped to what the region
///     holds) and the serving totals.
///   * minimal — the primary metric alone; the state dot when nothing is known.
///
/// A `nil` in the pushed state always renders as nothing or as `—`, never as a
/// zero or a green: `hostRows == nil` means the section is off or the read
/// failed, an empty `hostRows` means the route reported no hosts at all, and
/// those two say different things.
struct HSCCLiveActivityMonitor: Widget {
    var body: some WidgetConfiguration {
        let style = WidgetStyle.resolved(for: .liveActivityMonitor)
        return ActivityConfiguration(for: MonitorActivityAttributes.self) { context in
            LockScreenMonitorView(context: context)
                .widgetStyle(style)
                .activityBackgroundTint(style.backgroundColor)
                .activitySystemActionForegroundColor(style.primaryTextColor)
        } dynamicIsland: { context in
            let state = context.state
            return DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    MonitorStateDot(state: state.state, style: style)
                }
                DynamicIslandExpandedRegion(.center) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(MonitorSurfaceCopy.headline(state.state))
                            .font(style.font(.headline))
                            .foregroundColor(style.primaryTextColor)
                            .lineLimit(1)
                        if let primary = state.primary {
                            MonitorPrimaryLine(primary: primary, style: style, named: true)
                        }
                        if let feed = feedText(state) {
                            Text(feed)
                                .font(style.font(.caption2))
                                .foregroundColor(style.secondaryTextColor)
                                .lineLimit(1)
                        }
                        if let host = state.host {
                            Text(MonitorSurfaceCopy.hostSummary(host))
                                .font(style.font(.caption2))
                                .foregroundColor(style.secondaryTextColor)
                                .lineLimit(1)
                        }
                    }
                }
                DynamicIslandExpandedRegion(.trailing) {
                    VStack(alignment: .trailing, spacing: 2) {
                        Text(MonitorSurfaceCopy.tally(up: state.nodesUp, total: state.nodesTotal))
                            .font(style.font(size: 11, weight: .semibold))
                            .foregroundColor(MonitorStateColour.color(state.state, style: style))
                        if let secondary = state.secondary {
                            Text(secondary.value)
                                .font(style.font(size: 10, weight: .semibold))
                                .foregroundColor(MonitorLevelColour.color(secondary.level, style: style))
                                .lineLimit(1)
                        }
                    }
                }
                DynamicIslandExpandedRegion(.bottom) {
                    let rows = MonitorSurfaceReading.islandHostRows(state.hostRows ?? [])
                    let totals = MonitorSurfaceReading.islandServingTotals(state.servingTotals ?? [])
                    VStack(alignment: .leading, spacing: 3) {
                        if !rows.shown.isEmpty {
                            // The region's own clip PLUS what the push cap left
                            // out, so `+N more` is a statement about the fleet
                            // rather than about the list this region was handed.
                            MonitorHostTable(rows: rows.shown,
                                             dropped: rows.dropped + (state.hostRowsDropped ?? 0),
                                             dense: true,
                                             style: style)
                        }
                        ForEach(totals.shown) { total in
                            Text(MonitorSurfaceReading.servingTotalText(total, modelVisible: true))
                                .font(style.font(size: 9))
                                .foregroundColor(MonitorLevelColour.color(total.loadLevel, style: style))
                                .lineLimit(1)
                        }
                        if let more = MonitorSurfaceReading.moreText(
                            dropped: totals.dropped + (state.servingTotalsDropped ?? 0)) {
                            Text(more)
                                .font(style.font(size: 9))
                                .foregroundColor(style.secondaryTextColor)
                        }
                        if rows.shown.isEmpty, let aggregate = MonitorSurfaceReading.servingSummaryText(
                            running: state.servingRunning, waiting: state.servingWaiting) {
                            Text(aggregate)
                                .font(style.font(size: 9))
                                .foregroundColor(MonitorLevelColour.color(state.queueLevel, style: style))
                                .lineLimit(1)
                        }
                    }
                }
            } compactLeading: {
                if let primary = state.primary {
                    Text(primary.value)
                        .font(style.font(size: 11, weight: .semibold))
                        .foregroundColor(MonitorLevelColour.color(primary.level, style: style))
                        .lineLimit(1)
                } else {
                    // Nothing was told: the glyph says "monitor", it does not
                    // claim a reading.
                    Image(systemName: "gauge.with.dots.needle.bottom.50percent").widgetIcon(style)
                        .foregroundColor(style.secondaryTextColor)
                }
            } compactTrailing: {
                if let secondary = state.secondary {
                    Text(secondary.value)
                        .font(style.font(size: 11, weight: .semibold))
                        .foregroundColor(MonitorLevelColour.color(secondary.level, style: style))
                        .lineLimit(1)
                } else if state.primary?.kind != .tally {
                    // The tally is the last thing to fall back to, and only when
                    // the leading metric is not already the tally — the same
                    // number in both halves of a glance reads as two facts.
                    Text(MonitorSurfaceCopy.tally(up: state.nodesUp, total: state.nodesTotal))
                        .font(style.font(size: 11, weight: .semibold))
                        .foregroundColor(MonitorStateColour.color(state.state, style: style))
                }
            } minimal: {
                if let primary = state.primary {
                    Text(primary.value)
                        .font(style.font(size: 11, weight: .semibold))
                        .foregroundColor(MonitorLevelColour.color(primary.level, style: style))
                        .lineLimit(1)
                } else {
                    MonitorStateDot(state: state.state, style: style)
                }
            }
        }
    }

    /// The serving table's model + total rate, when the route had them.
    private func feedText(_ state: MonitorActivityAttributes.ContentState) -> String? {
        var parts: [String] = []
        if let model = state.nodes.compactMap({ $0.model }).first { parts.append(model) }
        if let gen = state.genTps { parts.append(String(format: "%.1f t/s", gen)) }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }
}

// MARK: - Shared copy + colour mapping

/// The words these surfaces use. Kept in one place because the Lock Screen and
/// the island must not describe one pushed state with two different headlines.
enum MonitorSurfaceCopy {
    static func headline(_ state: String) -> String {
        switch state {
        case "serving": return "Cluster serving"
        case "degraded": return "Cluster degraded"
        case "waking": return "Cluster waking"
        case "down": return "Cluster down"
        case "unreachable": return "Can't reach cluster"
        default: return "HSCC Monitor"
        }
    }

    /// The CLI's host tally: `4/4 up`, or `— up` when the monitor route did not
    /// answer. Never `0/0`.
    static func tally(up: Int?, total: Int?) -> String {
        FleetFormat.up(up, total)
    }

    static func hostSummary(_ host: HostMetric) -> String {
        var parts = [host.os, host.arch].compactMap { $0 }
        if let cpu = host.cpuPct { parts.append("CPU \(fmt(cpu))%") }
        if let mem = host.memPct { parts.append("RAM \(fmt(mem))%") }
        if let disk = host.diskPct { parts.append("Disk \(fmt(disk))%") }
        return parts.joined(separator: " · ")
    }

    private static func fmt(_ v: Double) -> String { String(format: "%.0f", v) }
}

/// `ClusterMetricLevel` → `WidgetStyle` colour. The verdict itself is the
/// view-model's (`ClusterMonitorViewModel`), so this switch is the only place a
/// threshold reaches a pixel, and it maps — it never compares numbers.
///
/// `.dim` and `.unknown` are deliberately different: `.dim` is the CLI's word
/// for "measured, and idle" and `.unknown` is "not told", and painting an
/// absent metric like an idle one is the false zero rule 1 forbids.
enum MonitorLevelColour {
    static func color(_ level: ClusterMetricLevel?, style: WidgetStyle) -> Color {
        switch level {
        case .critical: return style.badColor
        case .warn: return style.warnColor
        case .ok: return style.okColor
        case .dim: return style.secondaryTextColor
        case .unknown, .none: return style.secondaryTextColor.opacity(0.6)
        }
    }
}

/// The cluster state's colour — the reading of the whole activity, not of one
/// metric.
enum MonitorStateColour {
    static func color(_ state: String, style: WidgetStyle) -> Color {
        switch state {
        case "serving": return style.okColor
        case "degraded": return style.warnColor
        case "waking": return style.warnColor
        case "down": return style.badColor
        default: return style.secondaryTextColor   // unreachable / unknown
        }
    }
}

// MARK: - Shared pieces

/// The 10pt state dot, used by the expanded leading region and the minimal
/// fallback.
struct MonitorStateDot: View {
    let state: String
    let style: WidgetStyle

    var body: some View {
        Circle()
            .fill(MonitorStateColour.color(state, style: style))
            .frame(width: 10, height: 10)
    }
}

/// The primary metric: its value, coloured by its verdict, and — for the
/// surfaces with room — the host it came from. A per-host number without its
/// host reads like a fleet number, so `named` is true wherever there is width.
struct MonitorPrimaryLine: View {
    let primary: MonitorPrimaryMetric
    let style: WidgetStyle
    let named: Bool

    var body: some View {
        HStack(spacing: 4) {
            Text(primary.value)
                .font(style.font(size: 12, weight: .semibold))
                .foregroundColor(MonitorLevelColour.color(primary.level, style: style))
                .lineLimit(1)
            if named, let label = primary.label, !label.isEmpty {
                Text(label)
                    .font(style.font(size: 9))
                    .foregroundColor(style.secondaryTextColor)
                    .lineLimit(1)
            }
        }
    }
}

/// The host table. The column headers ARE `CLIMonitorHostRow.columns`' names
/// (through `MonitorSurfaceReading.detailHostColumns`), so a header can never
/// label a different column than the cell under it, and a surface cannot
/// retype a column name and get it wrong.
///
/// Both the Lock Screen and the island's bottom region draw this, `dense` for
/// the tighter one. A pushed row list is untrusted input to a fixed-height
/// region, so the caller clips it (`MonitorSurfaceReading.lockScreenHostRows` /
/// `islandHostRows`) and hands the count it dropped here, which the table says
/// out loud instead of letting a short table read like the whole fleet.
struct MonitorHostTable: View {
    let rows: [MonitorHostMetric]
    let dropped: Int
    let dense: Bool
    let style: WidgetStyle

    private var labelWidth: CGFloat { dense ? 46 : 58 }
    private var servingWidth: CGFloat { dense ? 56 : 74 }

    var body: some View {
        VStack(alignment: .leading, spacing: dense ? 1 : 2) {
            header
            // Indexed deliberately: the host's wire address is the only
            // naturally-unique key, and pushing it into the activity's stored
            // state is what §7 forbids, so the row list is keyed by position.
            ForEach(rows.indices, id: \.self) { index in
                MonitorHostRowView(row: rows[index], dense: dense, style: style)
            }
            if let more = MonitorSurfaceReading.moreText(dropped: dropped) {
                Text(more)
                    .font(style.font(size: 9))
                    .foregroundColor(style.secondaryTextColor)
            }
        }
    }

    private var header: some View {
        HStack(spacing: dense ? 4 : 6) {
            Text("Host")
                .font(style.font(size: dense ? 8 : 9, weight: .semibold))
                .foregroundColor(style.secondaryTextColor)
                .frame(width: labelWidth, alignment: .leading)
            ForEach(MonitorSurfaceReading.detailHostColumns.indices, id: \.self) { index in
                Text(MonitorSurfaceReading.detailHostColumns[index])
                    .font(style.font(size: dense ? 8 : 9, weight: .semibold))
                    .foregroundColor(style.secondaryTextColor)
                    .frame(maxWidth: .infinity, alignment: .trailing)
                    .lineLimit(1)
                    .minimumScaleFactor(0.7)
            }
            Text(MonitorSurfaceReading.runColumnName)
                .font(style.font(size: dense ? 8 : 9, weight: .semibold))
                .foregroundColor(style.secondaryTextColor)
                .frame(width: servingWidth, alignment: .trailing)
                .lineLimit(1)
        }
    }
}

/// One host's row. The cells are the CLI's strings; the colour behind each is
/// the view-model's verdict for that column.
struct MonitorHostRowView: View {
    let row: MonitorHostMetric
    let dense: Bool
    let style: WidgetStyle

    private var labelWidth: CGFloat { dense ? 46 : 58 }
    private var servingWidth: CGFloat { dense ? 56 : 74 }

    var body: some View {
        HStack(spacing: dense ? 4 : 6) {
            Text(row.label)
                .font(style.font(size: dense ? 9 : 11, weight: .semibold))
                .foregroundColor(row.isFailure ? style.secondaryTextColor : style.primaryTextColor)
                .frame(width: labelWidth, alignment: .leading)
                .lineLimit(1)
            if row.isFailure {
                // The CLI puts the reason in the CPU % column and dashes in the
                // rest; this surface has no CPU % column, and the raw reason is
                // the route's string rather than this app's, so it says
                // "not sampled" and stays honest without that reason (§7).
                Text(MonitorSurfaceReading.notSampledText)
                    .font(style.font(size: dense ? 9 : 10))
                    .foregroundColor(style.secondaryTextColor)
                    .frame(maxWidth: .infinity, alignment: .trailing)
                    .lineLimit(1)
            } else {
                let cells = MonitorSurfaceReading.detailCells(row)
                let levels = MonitorSurfaceReading.detailLevels(row)
                ForEach(cells.indices, id: \.self) { index in
                    Text(cells[index])
                        .font(style.font(size: dense ? 9 : 10))
                        .foregroundColor(MonitorLevelColour.color(levels[index], style: style))
                        .frame(maxWidth: .infinity, alignment: .trailing)
                        .lineLimit(1)
                        .minimumScaleFactor(0.7)
                }
            }
            Text(MonitorSurfaceReading.servingText(running: row.running, waiting: row.waiting) ?? "—")
                .font(style.font(size: dense ? 9 : 10))
                .foregroundColor(MonitorLevelColour.color(row.servingLevel, style: style))
                .frame(width: servingWidth, alignment: .trailing)
                .lineLimit(1)
        }
    }
}

// MARK: - Lock Screen / banner view

/// The Lock Screen (and banner) presentation of the monitor activity: the
/// state header, then the host table, then the serving totals, then the
/// daemon-host line. Sections honour the settings showSections (state / nodes /
/// host); a hidden section pushes nothing, so nothing is drawn for it.
struct LockScreenMonitorView: View {
    @Environment(\.widgetStyle) private var style
    let context: ActivityViewContext<MonitorActivityAttributes>

    private var state: MonitorActivityAttributes.ContentState { context.state }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            header

            if let feed = feedText {
                Text(feed)
                    .font(style.font(.caption))
                    .foregroundColor(style.secondaryTextColor)
                    .lineLimit(1)
            }

            hostSection

            servingSection

            if let host = state.host {
                HStack(spacing: 8) {
                    Image(systemName: "desktopcomputer").widgetIcon(style)
                        .foregroundColor(style.secondaryTextColor)
                    Text(MonitorSurfaceCopy.hostSummary(host))
                        .font(style.font(.caption))
                        .foregroundColor(style.secondaryTextColor)
                    Spacer()
                    if host.daemonRunning {
                        Text("daemon up")
                            .font(style.font(.caption2))
                            .foregroundColor(style.okColor)
                    } else {
                        Text("daemon down")
                            .font(style.font(.caption2))
                            .foregroundColor(style.badColor)
                    }
                }
            }
        }
        .padding()
    }

    private var header: some View {
        HStack(spacing: 8) {
            MonitorStateDot(state: state.state, style: style)
            Text(MonitorSurfaceCopy.headline(state.state))
                .font(style.font(.headline))
                .foregroundColor(style.primaryTextColor)
            if let primary = state.primary {
                MonitorPrimaryLine(primary: primary, style: style, named: true)
            }
            Spacer()
            Text(MonitorSurfaceCopy.tally(up: state.nodesUp, total: state.nodesTotal))
                .font(style.font(.subheadline, weight: .semibold))
                .foregroundColor(MonitorStateColour.color(state.state, style: style))
            Text(ageString(state.updatedAt))
                .font(style.font(.caption2))
                .foregroundColor(style.secondaryTextColor)
        }
        .widgetHeader(style)
    }

    /// The host table, or — for a state an older app pushed, before the tables
    /// existed — the per-node meters it did have. A nil `hostRows` is "this
    /// push has no table", and an EMPTY one is "the route reported no hosts",
    /// which is the CLI's `0 host(s)` line rather than the fallback path.
    @ViewBuilder private var hostSection: some View {
        if let rows = state.hostRows {
            if rows.isEmpty {
                Text("0 hosts reported")
                    .font(style.font(.caption))
                    .foregroundColor(style.secondaryTextColor)
            } else {
                let shown = MonitorSurfaceReading.lockScreenHostRows(rows)
                MonitorHostTable(rows: shown.shown,
                                 dropped: shown.dropped + (state.hostRowsDropped ?? 0),
                                 dense: false,
                                 style: style)
            }
        } else if !state.nodes.isEmpty {
            // Legacy fallback (pre-t_1685f884 ContentState). Its meters colour
            // themselves, because that payload carries no pushed verdicts;
            // anything pushed by the current app goes through the table above.
            VStack(alignment: .leading, spacing: 6) {
                ForEach(state.nodes.prefix(4), id: \.label) { node in
                    MonitorNodeRow(node: node)
                }
            }
        } else if state.host == nil {
            Text("Waiting for first monitor data…")
                .font(style.font(.caption))
                .foregroundColor(style.secondaryTextColor)
        }
    }

    /// The serving half: the aggregate Run/Wait the table reported, then the
    /// CLI's per-model `total (n)` rows. A nil section draws nothing — silence
    /// is not "0 models".
    @ViewBuilder private var servingSection: some View {
        let aggregate = MonitorSurfaceReading.servingSummaryText(running: state.servingRunning,
                                                                waiting: state.servingWaiting)
        let totals = MonitorSurfaceReading.lockScreenServingTotals(state.servingTotals ?? [])
        if aggregate != nil || !totals.shown.isEmpty {
            VStack(alignment: .leading, spacing: 2) {
                if let aggregate {
                    Text(aggregate)
                        .font(style.font(.caption))
                        .foregroundColor(MonitorLevelColour.color(state.queueLevel, style: style))
                        .lineLimit(1)
                }
                ForEach(totals.shown) { total in
                    HStack(spacing: 6) {
                        Text(MonitorSurfaceReading.servingTotalText(total, modelVisible: true))
                            .font(style.font(.caption2))
                            .foregroundColor(MonitorLevelColour.color(total.loadLevel, style: style))
                            .lineLimit(1)
                        Spacer(minLength: 0)
                        Text(total.genTps)
                            .font(style.font(.caption2))
                            .foregroundColor(style.secondaryTextColor)
                    }
                }
                if let more = MonitorSurfaceReading.moreText(
                    dropped: totals.dropped + (state.servingTotalsDropped ?? 0)) {
                    Text(more)
                        .font(style.font(.caption2))
                        .foregroundColor(style.secondaryTextColor)
                }
            }
        }
    }

    private var feedText: String? {
        var parts: [String] = []
        if let model = state.nodes.compactMap({ $0.model }).first { parts.append(model) }
        if let gen = state.genTps { parts.append(String(format: "%.1f t/s", gen)) }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    private func ageString(_ date: Date) -> String {
        let total = Int(Date().timeIntervalSince(date))
        if total < 60 { return "now" }
        return "\(total / 60)m ago"
    }
}

// MARK: - Per-node meter row (legacy fallback only)

/// One node row of the PRE-t_1685f884 readout: short label + three meters
/// (CPU / RAM / GPU util), each coloured by util. Only a ContentState pushed by
/// an older app version reaches here — a current push has `hostRows` and draws
/// `MonitorHostTable` instead. A nil metric renders a muted dash rather than a
/// fabricated reading.
struct MonitorNodeRow: View {
    @Environment(\.widgetStyle) private var style
    let node: NodeMetric

    var body: some View {
        HStack(spacing: 8) {
            VStack(alignment: .leading, spacing: 1) {
                Text(node.label)
                    .font(style.font(size: 11, weight: .semibold))
                    .foregroundColor(style.primaryTextColor)
                if let model = node.model {
                    Text(model)
                        .font(style.font(size: 9))
                        .foregroundColor(style.secondaryTextColor)
                        .lineLimit(1)
                }
            }
            .frame(width: 70, alignment: .leading)
            MBar(value: node.cpuPct, icon: "cpu")
            MBar(value: node.memPct, icon: "memorychip")
            MBar(value: node.gpuUtilPct, icon: "gpu")
            VStack(alignment: .trailing, spacing: 1) {
                if let gpuTemp = node.gpuTempC {
                    Text("GPU \(Int(gpuTemp.rounded()))°C")
                        .font(style.font(size: 9))
                        .foregroundColor(style.secondaryTextColor)
                }
                if let cpuTemp = node.cpuTempC {
                    Text("CPU \(Int(cpuTemp.rounded()))°C")
                        .font(style.font(size: 9))
                        .foregroundColor(style.secondaryTextColor)
                }
            }
            .frame(width: 58, alignment: .trailing)
        }
    }
}

/// One small labelled meter: a filled fraction bar + its percent readout.
struct MBar: View {
    @Environment(\.widgetStyle) private var style
    let value: Double?
    let icon: String

    var body: some View {
        HStack(spacing: 3) {
            Image(systemName: icon).widgetIcon(style)
                .font(style.font(size: 9))
                .foregroundColor(style.secondaryTextColor)
            if let value {
                GeometryReader { geo in
                    ZStack(alignment: .leading) {
                        Capsule().fill(style.secondaryTextColor.opacity(0.25))
                        Capsule().fill(meterColor(value))
                            .frame(width: geo.size.width * CGFloat(min(max(value, 0), 100) / 100))
                    }
                }
                .frame(height: 6)
                Text(fmt(value))
                    .font(style.font(size: 9))
                    .foregroundColor(meterColor(value))
                    .frame(width: 26, alignment: .trailing)
            } else {
                Text("—")
                    .font(style.font(size: 9))
                    .foregroundColor(style.secondaryTextColor)
            }
        }
        .frame(maxWidth: .infinity)
    }

    private func meterColor(_ v: Double) -> Color {
        if v >= 90 { return style.badColor }
        if v >= 70 { return style.warnColor }
        return style.okColor
    }

    private func fmt(_ v: Double) -> String {
        String(format: "%.0f", v)
    }
}
