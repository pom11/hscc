import SwiftUI
import WidgetKit

// ---------------------------------------------------------------------------
// HSCC Monitor widget views — small + medium + large, plus the three Lock Screen
// accessory families.
// ---------------------------------------------------------------------------

/// The Home Screen "HSCC Monitor" widget: `hscc cluster monitor` per-node
/// metrics + the daemon-host machine data, refreshed on the configured cadence.
struct MonitorWidget: Widget {
    let kind = "MonitorWidget"

    var body: some WidgetConfiguration {
        let style = WidgetStyle.resolved(for: .widgetMonitor)
        return StaticConfiguration(kind: kind, provider: MonitorTimelineProvider()) { entry in
            MonitorWidgetView(entry: entry)
                .containerBackground(for: .widget) {
                    WidgetSurfaceBackground(style: style)
                }
                .widgetRenderingAdaptation(style: style)
        }
        .configurationDisplayName("HSCC Monitor")
        .description("Cluster monitor metrics and the machine HSCC runs on.")
        // The macOS desktop widget (t_1143c19b) also offers systemExtraLarge:
        // the per-node metric rows are what gains from the extra width.
        #if os(iOS)
        .supportedFamilies([.systemSmall, .systemMedium, .systemLarge,
                            .accessoryInline, .accessoryCircular, .accessoryRectangular])
        #else
        .supportedFamilies([.systemSmall, .systemMedium, .systemLarge,
                            .systemExtraLarge])
        #endif
    }
}

/// The single view rendered for every family, choosing layout by size.
struct MonitorWidgetView: View {
    @Environment(\.widgetStyle) private var style
    let entry: MonitorEntry
    @Environment(\.widgetFamily) private var family

    var body: some View {
        switch family {
        case .systemSmall:
            SmallMonitorWidget(entry: entry)
        #if os(iOS)
        case .accessoryInline:
            InlineMonitorWidget(entry: entry)
        case .accessoryCircular:
            CircularMonitorWidget(entry: entry)
        case .accessoryRectangular:
            RectangularMonitorWidget(entry: entry)
        #endif
        case .systemLarge:
            LargeMonitorWidget(entry: entry)
        case .systemExtraLarge:
            LargeMonitorWidget(entry: entry)
        default:
            MediumMonitorWidget(entry: entry)
        }
    }
}

extension WidgetFamily {
    /// True for the families that can draw the daemon-host strip/block. The Lock
    /// Screen accessories draw per-node numbers only, so their refresh must not
    /// spend `/v1/daemon/host` on pixels that cannot exist (§2's budget).
    /// Positive-listed for the same reason as `showsBoardWork`: WidgetKit marks
    /// the `accessory*` cases unavailable on the host, and this decision has to
    /// stay executable.
    var showsDaemonHost: Bool {
        switch self {
        case .systemSmall, .systemMedium, .systemLarge, .systemExtraLarge:
            return true
        default:
            return false
        }
    }
}

// ---------------------------------------------------------------------------
// Shared building blocks
// ---------------------------------------------------------------------------

/// Honest rendition of the gate states (unconfigured / paused / unreachable).
/// The unreachable pane says whether a last-known snapshot exists and how old it
/// is — a stale read is never presented as live data, and no stale node row is
/// drawn at all, so the only thing the container hands back to a view is the
/// projected node COUNT plus the daemon-host block (§7).
extension MonitorEntry {
    /// The label the Lock Screen accessory families draw for a gate state.
    /// `configured` is checked FIRST, so an unconfigured device can never read
    /// as "paused" (audit-3 / t_5c554c5b).
    var shortGateLabel: String {
        if !configured { return "Set up in HSCC" }
        return "Paused"
    }

    /// The node the compact families draw: the first node with a sample (the
    /// medium grid draws every node, so this is a subset of what it already
    /// has), else the first node so its error can still be named.
    var headlineNode: MonitorNode? {
        nodes.first(where: { $0.sample != nil }) ?? nodes.first
    }

    /// Fleet CPU: the mean of the per-node values the medium grid already draws.
    /// nil when no node reported one — never a fabricated 0.
    var averageCPU: Double? {
        let values = nodes.compactMap { $0.sample?.cpuUsagePct }
        guard !values.isEmpty else { return nil }
        return values.reduce(0, +) / Double(values.count)
    }

    /// `accessoryInline`: one short line of the same node numbers the medium
    /// view draws. A gate state, an unreachable read and a missing sample each
    /// say so in words instead of rendering a zero.
    var inlineLine: String {
        if !configured || paused { return shortGateLabel }
        if state == .unreachable {
            return lastKnownAgeMinutes.map { "Can't reach · \($0)m" } ?? "Can't reach"
        }
        guard showSections.contains("nodes") else { return state.label }
        guard let node = headlineNode else { return "no data" }
        guard let sample = node.sample else { return "no sample" }
        var parts: [String] = []
        if let cpu = sample.cpuUsagePct { parts.append("CPU \(monitorPercent(cpu))") }
        if let gpu = sample.gpuUtilPct { parts.append("GPU \(monitorPercent(gpu))") }
        return parts.isEmpty ? "no sample" : parts.joined(separator: " · ")
    }

    @ViewBuilder
    func gateView(style: WidgetStyle) -> some View {
        if !configured {
            MonitorGateView(icon: "gearshape",
                            title: state.label,
                            caption: "Add host, port, and token in Settings.")
        } else if paused {
            MonitorGateView(icon: "pause.circle",
                            title: state.label,
                            caption: "Turn this widget back on in Settings.")
        } else if state == .unreachable {
            unreachablePane(style: style)
        } else {
            EmptyView()
        }
    }

    /// The unreachable pane — the last-known snapshot dimmed for stale + age.
    @ViewBuilder
    private func unreachablePane(style: WidgetStyle) -> some View {
        VStack(alignment: .leading, spacing: style.spacing(Theme.Spacing.sm.rawValue)) {
            HStack(spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
                Circle().fill(state.color(style)).frame(width: 8, height: 8)
                Text(state.label)
                    .font(style.font(.subheadline, weight: .semibold))
                    .foregroundColor(style.primaryTextColor)
                Spacer()
            }
            if lastKnownNodeCount > 0 {
                Text("showing last-known data")
                    .font(style.font(.caption2))
                    .foregroundColor(style.secondaryTextColor)
            }
            if let age = lastKnownAgeMinutes {
                Text("\(age) min ago")
                    .font(style.font(.caption2))
                    .foregroundColor(style.secondaryTextColor)
            }
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// The full-pane unconfigured / paused gate.
struct MonitorGateView: View {
    @Environment(\.widgetStyle) private var style
    let icon: String
    let title: String
    let caption: String

    var body: some View {
        VStack(alignment: .leading, spacing: style.spacing(Theme.Spacing.sm.rawValue)) {
            Image(systemName: icon).widgetIcon(style)
                .font(style.font(.title3))
                .foregroundColor(style.secondaryTextColor)
            Text(title)
                .font(style.font(.caption))
                .foregroundColor(style.secondaryTextColor)
            Text(caption)
                .font(style.font(.caption2))
                .foregroundColor(style.secondaryTextColor)
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// "42%" — a utilisation percent, rounded. nil is the em dash, never `0`.
private func monitorPercent(_ value: Double?) -> String {
    guard let value else { return "—" }
    return "\(Int(value.rounded()))%"
}

/// A single labelled meter: label left, thin bar, percent right. The bar colour
/// is honest — derived from the value against warn/bad thresholds (CPU>85 warn,
/// >95 bad; other gauges at >90 warn, >95 bad). Dims for stale last-known data.
struct MonitorMeter: View {
    @Environment(\.widgetStyle) private var style
    let label: String
    let value: Double?
    /// True for a CPU meter (uses the 85 warn threshold; others 90).
    var isCPU = false
    var dimmed = false

    var body: some View {
        HStack(spacing: style.spacing(Theme.Spacing.sm.rawValue)) {
            Text(label)
                .font(style.font(size: 10))
                .foregroundColor(dimmed ? style.secondaryTextColor : style.primaryTextColor)
                .frame(width: 30, alignment: .leading)
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule()
                        .fill(dimmed ? style.secondaryTextColor.opacity(0.22) : style.secondaryTextColor.opacity(0.22))
                    if let value {
                        Capsule()
                            .fill(dimmed ? style.secondaryTextColor.opacity(0.5) : Self.color(for: value, isCPU: isCPU, style: style))
                            .frame(width: max(geo.size.width * CGFloat(min(max(value / 100, 0), 1)), 3))
                    }
                }
            }
            .frame(height: 6)
            Text(valueText)
                .font(style.font(size: 10))
                .foregroundColor(dimmed ? style.secondaryTextColor : style.primaryTextColor)
                .frame(width: 30, alignment: .trailing)
        }
    }

    private var valueText: String { monitorPercent(value) }

    /// Honest data-colouring: CPU>85 warn, >95 bad; other gauges >90 warn, >95 bad.
    static func color(for value: Double, isCPU: Bool, style: WidgetStyle) -> Color {
        let warn = isCPU ? 85.0 : 90.0
        let bad = 95.0
        if value > bad { return style.badColor }
        if value > warn { return style.warnColor }
        return style.okColor
    }
}

// The displayed node label (`displayLabel` — sample hostname, else the address
// tail) lives on `MonitorNode` itself in Sources/Shared/SharedModels.swift, so
// the widget, the Live Activity and the live probe share one rule (t_595a5fee).

// ---------------------------------------------------------------------------
// Medium — state line + a compact 2×2 node grid + a slim daemon-host strip
// ---------------------------------------------------------------------------

struct MediumMonitorWidget: View {
    @Environment(\.widgetStyle) private var style
    let entry: MonitorEntry

    var body: some View {
        if !entry.configured || entry.paused {
            entry.gateView(style: style)
        } else if entry.state == .unreachable {
            entry.gateView(style: style)
        } else {
            VStack(alignment: .leading, spacing: style.spacing(Theme.Spacing.sm.rawValue)) {
                stateLine
                if showNodes && !entry.nodes.isEmpty {
                    nodeGrid
                }
                if showHost, let host = entry.daemonHost {
                    HostStrip(host: host)
                }
            }
        }
    }

    private var showNodes: Bool { entry.showSections.contains("nodes") }
    private var showHost: Bool { entry.showSections.contains("host") }

    private var stateLine: some View {
        HStack(spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
            Circle().fill(entry.state.color(style)).frame(width: 8, height: 8)
            Text(entry.state.label)
                .font(style.font(.headline))
                .foregroundColor(style.primaryTextColor)
            Spacer()
            if let age = entry.lastKnownAgeMinutes {
                Text("\(age)m")
                    .font(style.font(.caption2))
                    .foregroundColor(style.secondaryTextColor)
            }
        }
    .widgetHeader(style)
    }

    /// 2×2 compact node grid — each cell: host label + CPU/RAM/GPU bars.
    private var nodeGrid: some View {
        LazyVGrid(columns: [GridItem(.flexible(), spacing: style.spacing(Theme.Spacing.md.rawValue)),
                            GridItem(.flexible(), spacing: style.spacing(Theme.Spacing.md.rawValue))],
                  spacing: style.spacing(Theme.Spacing.sm.rawValue)) {
            ForEach(entry.nodes) { node in
                MediumNodeCell(node: node)
            }
        }
    }
}

/// A compact node cell for the medium grid: host label + three tiny bars.
struct MediumNodeCell: View {
    @Environment(\.widgetStyle) private var style
    let node: MonitorNode
    private var sample: MonitorSample? { node.sample }

    var body: some View {
        VStack(alignment: .leading, spacing: style.spacing(Theme.Spacing.xxs.rawValue)) {
            nodeHeader
            if let sample {
                MonitorMeter(label: "CPU", value: sample.cpuUsagePct, isCPU: true)
                MonitorMeter(label: "RAM", value: sample.memUsedPct)
                MonitorMeter(label: "GPU", value: sample.gpuUtilPct)
            } else if let error = node.error {
                Text(error).font(style.font(.caption2)).foregroundColor(style.badColor).lineLimit(2)
            } else {
                Text("no sample").font(style.font(.caption2)).foregroundColor(style.secondaryTextColor)
            }
        }
    }

    private var nodeHeader: some View {
        HStack(spacing: style.spacing(Theme.Spacing.xxs.rawValue)) {
            Circle()
                .fill(node.sample?.cpuUsagePct.map { MonitorMeter.color(for: $0, isCPU: true, style: style) } ?? style.secondaryTextColor)
                .frame(width: 6, height: 6)
            Text(node.displayLabel)
                .font(style.font(size: 10, weight: .semibold))
                .foregroundColor(style.primaryTextColor)
                .lineLimit(1)
        }
    }
}

// ---------------------------------------------------------------------------
// Small — state line + the first node's CPU/RAM/GPU (or the daemon-host strip
// when the node section is off)
// ---------------------------------------------------------------------------

struct SmallMonitorWidget: View {
    @Environment(\.widgetStyle) private var style
    let entry: MonitorEntry

    var body: some View {
        if !entry.configured || entry.paused {
            entry.gateView(style: style)
        } else if entry.state == .unreachable {
            entry.gateView(style: style)
        } else {
            VStack(alignment: .leading, spacing: style.spacing(Theme.Spacing.sm.rawValue)) {
                stateLine
                if showNodes, let node = entry.headlineNode {
                    MediumNodeCell(node: node)
                } else if showHost, let host = entry.daemonHost {
                    HostStrip(host: host)
                }
                Spacer(minLength: 0)
            }
        }
    }

    private var showNodes: Bool { entry.showSections.contains("nodes") }
    private var showHost: Bool { entry.showSections.contains("host") }

    private var stateLine: some View {
        HStack(spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
            Circle().fill(entry.state.color(style)).frame(width: 8, height: 8)
            Text(entry.state.label)
                .font(style.font(.subheadline, weight: .semibold))
                .foregroundColor(style.primaryTextColor)
                .lineLimit(1)
                .minimumScaleFactor(0.8)
            Spacer(minLength: 0)
            if let age = entry.lastKnownAgeMinutes {
                Text("\(age)m")
                    .font(style.font(.caption2))
                    .foregroundColor(style.secondaryTextColor)
            }
        }
    .widgetHeader(style)
    }
}

/// A slim daemon-host strip for the medium widget: hostname + CPU/RAM/DISK bars.
struct HostStrip: View {
    @Environment(\.widgetStyle) private var style
    let host: MonitorDaemonHost
    var dimmed = false

    var body: some View {
        VStack(alignment: .leading, spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
            HStack(spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
                Image(systemName: "laptopcomputer").widgetIcon(style)
                    .font(style.font(size: 10))
                    .foregroundColor(style.secondaryTextColor)
                Text(hostTitle)
                    .font(style.font(size: 11, weight: .semibold))
                    .foregroundColor(style.primaryTextColor)
                    .lineLimit(1)
                Spacer()
                if host.daemon_running == true {
                    Circle().fill(style.okColor).frame(width: 5, height: 5)
                }
            }
            HStack(spacing: style.spacing(Theme.Spacing.md.rawValue)) {
                MonitorMeter(label: "CPU", value: host.cpuPercent, isCPU: true)
                MonitorMeter(label: "RAM", value: host.memPercent)
                MonitorMeter(label: "DISK", value: host.diskPercent)
            }
        }
    }

    private var hostTitle: String {
        guard let hostname = host.hostname, !hostname.isEmpty else { return "Daemon host" }
        return hostname
    }
}

// ---------------------------------------------------------------------------
// Large — state line + a full per-node table + a complete daemon-host block
// ---------------------------------------------------------------------------

struct LargeMonitorWidget: View {
    @Environment(\.widgetStyle) private var style
    let entry: MonitorEntry

    var body: some View {
        if !entry.configured || entry.paused {
            entry.gateView(style: style)
        } else if entry.state == .unreachable {
            entry.gateView(style: style)
        } else {
            VStack(alignment: .leading, spacing: style.spacing(Theme.Spacing.sm.rawValue)) {
                stateLine
                if showNodes && !entry.nodes.isEmpty {
                    nodesTable
                }
                if showHost, let host = entry.daemonHost {
                    HostBlock(host: host)
                }
                Spacer(minLength: 0)
            }
        }
    }

    private var showNodes: Bool { entry.showSections.contains("nodes") }
    private var showHost: Bool { entry.showSections.contains("host") }

    private var stateLine: some View {
        HStack(spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
            Circle().fill(entry.state.color(style)).frame(width: 8, height: 8)
            Text(entry.state.label)
                .font(style.font(.headline))
                .foregroundColor(style.primaryTextColor)
            Spacer()
            if let age = entry.lastKnownAgeMinutes {
                Text("\(age) min ago")
                    .font(style.font(.caption2))
                    .foregroundColor(style.secondaryTextColor)
            }
        }
    .widgetHeader(style)
    }

    private var nodesTable: some View {
        VStack(alignment: .leading, spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
            ForEach(entry.nodes) { node in
                LargeNodeRow(node: node)
            }
        }
    }
}

/// A full per-node row for the large widget: label + CPU/GPU/RAM meters.
struct LargeNodeRow: View {
    @Environment(\.widgetStyle) private var style
    let node: MonitorNode
    private var sample: MonitorSample? { node.sample }

    var body: some View {
        VStack(alignment: .leading, spacing: style.spacing(Theme.Spacing.xxs.rawValue)) {
            header
            if let sample {
                MonitorMeter(label: "CPU", value: sample.cpuUsagePct, isCPU: true)
                MonitorMeter(label: "GPU", value: sample.gpuUtilPct)
                MonitorMeter(label: "RAM", value: sample.memUsedPct)
            } else if let error = node.error {
                Text(error)
                    .font(style.font(.caption2))
                    .foregroundColor(style.badColor)
                    .lineLimit(2)
            } else {
                Text("no sample")
                    .font(style.font(.caption2))
                    .foregroundColor(style.secondaryTextColor)
            }
        }
    }

    private var header: some View {
        HStack(spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
            Circle()
                .fill((sample?.cpuUsagePct).map { MonitorMeter.color(for: $0, isCPU: true, style: style) } ?? style.secondaryTextColor)
                .frame(width: 6, height: 6)
            Text(node.displayLabel)
                .font(style.font(size: 11, weight: .semibold))
                .foregroundColor(style.primaryTextColor)
            Spacer()
            if let u = sample?.uptimeText {
                Text(u)
                    .font(style.font(size: 9))
                    .foregroundColor(style.secondaryTextColor)
            }
        }
    }
}

/// The full daemon-host block for the large widget: hostname line + gauges.
struct HostBlock: View {
    @Environment(\.widgetStyle) private var style
    let host: MonitorDaemonHost
    var dimmed = false

    var body: some View {
        VStack(alignment: .leading, spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
            HStack(spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
                Image(systemName: "laptopcomputer").widgetIcon(style)
                    .font(style.font(size: 11))
                    .foregroundColor(style.secondaryTextColor)
                Text(hostTitle)
                    .font(style.font(size: 12, weight: .semibold))
                    .foregroundColor(style.primaryTextColor)
                    .lineLimit(1)
                Spacer()
                daemonStatus
            }
            MonitorMeter(label: "CPU", value: host.cpuPercent, isCPU: true)
            MonitorMeter(label: "RAM", value: host.memPercent)
            MonitorMeter(label: "DISK", value: host.diskPercent)
            if let line = processLine {
                Text(line)
                    .font(style.font(size: 9))
                    .foregroundColor(style.secondaryTextColor)
            }
        }
    }

    private var hostTitle: String {
        var s = host.hostname ?? "Daemon host"
        if let platform = host.platform { s += " · \(platform)" }
        if let arch = host.arch { s += " \(arch)" }
        return s
    }

    /// "N processes" (+ " · up <uptime>") as a single line, or nil when neither
    /// is present. Computed outside the ViewBuilder so the `if let` is a plain
    /// View conditional (no statement-block inference surprises).
    private var processLine: String? {
        guard let processes = host.processes else {
            return host.uptimeText.map { "up \($0)" }
        }
        var line = "\(processes) processes"
        if let uptime = host.uptimeText { line += " · up \(uptime)" }
        return line
    }

    @ViewBuilder
    private var daemonStatus: some View {
        if host.daemon_running == true {
            HStack(spacing: 3) {
                Circle().fill(style.okColor).frame(width: 5, height: 5)
                Text("daemon up")
                    .font(style.font(size: 9))
                    .foregroundColor(style.secondaryTextColor)
            }
        } else {
            Text("daemon down")
                .font(style.font(size: 9))
                .foregroundColor(style.badColor)
        }
    }
}

// ---------------------------------------------------------------------------
// Lock Screen accessories — one line inline, one CPU gauge circular, two lines
// rectangular. Each draws only the per-node numbers the medium view already
// has; the daemon-host half is neither fetched nor drawn here (§2's budget).
// ---------------------------------------------------------------------------

/// The Lock Screen inline: the first node's CPU and GPU, or the gate label.
struct InlineMonitorWidget: View {
    @Environment(\.widgetStyle) private var style
    let entry: MonitorEntry

    var body: some View {
        if !entry.configured || entry.paused {
            Text(entry.shortGateLabel)
                .font(style.font(.caption))
                .accessibilityLabel(entry.shortGateLabel)
        } else {
            Text(entry.inlineLine)
                .font(style.font(.caption))
                .accessibilityLabel(entry.inlineLine)
        }
    }
}

/// The Lock Screen circular: the fleet CPU the medium grid already draws, as a
/// gauge. A read with no sample renders a glyph or `—`, never a false 0.
struct CircularMonitorWidget: View {
    @Environment(\.widgetStyle) private var style
    let entry: MonitorEntry

    var body: some View {
        if !entry.configured || entry.paused {
            Text(entry.shortGateLabel)
                .font(style.font(size: 9, weight: .semibold))
                .foregroundColor(.secondary)
                .minimumScaleFactor(0.6)
                .accessibilityLabel(entry.shortGateLabel)
        } else if entry.state == .unreachable {
            Image(systemName: "exclamationmark.triangle.fill").widgetIcon(style)
                .font(style.font(.body))
                .foregroundColor(.primary)
                .accessibilityLabel(entry.state.label)
        } else if entry.showSections.contains("nodes"), let cpu = entry.averageCPU {
            Gauge(value: min(max(cpu, 0), 100), in: 0...100) {
                Text("CPU")
            } currentValueLabel: {
                Text(monitorPercent(cpu))
                    .font(style.font(size: 11, weight: .semibold))
            }
            .gaugeStyle(.accessoryCircularCapacity)
            .tint(MonitorMeter.color(for: cpu, isCPU: true, style: style))
            .accessibilityLabel("CPU \(monitorPercent(cpu))")
        } else if !entry.showSections.contains("nodes") {
            Text(entry.state.label)
                .font(style.font(size: 9, weight: .semibold))
                .foregroundColor(entry.state.color(style))
                .minimumScaleFactor(0.6)
                .accessibilityLabel(entry.state.label)
        } else {
            Text("—")
                .font(style.font(size: 11, weight: .semibold))
                .foregroundColor(.secondary)
                .accessibilityLabel("no sample")
        }
    }
}

/// The Lock Screen rectangular: the first node's label plus its CPU/MEM, or the
/// unreachable state with the age of the last-known snapshot.
struct RectangularMonitorWidget: View {
    @Environment(\.widgetStyle) private var style
    let entry: MonitorEntry

    var body: some View {
        VStack(alignment: .leading, spacing: style.spacing(Theme.Spacing.xxs.rawValue)) {
            if !entry.configured || entry.paused {
                Text(entry.shortGateLabel)
                    .font(style.font(size: 11, weight: .semibold))
                    .foregroundColor(.secondary)
                    .lineLimit(1)
                Text("Open HSCC to configure the cluster")
                    .font(style.font(size: 9))
                    .foregroundColor(.secondary)
                    .lineLimit(1)
            } else if entry.state == .unreachable {
                Text(entry.state.label).widgetHeader(style)
                    .font(style.font(size: 11, weight: .semibold))
                    .foregroundColor(.primary)
                    .lineLimit(1)
                Text(entry.lastKnownAgeMinutes.map { "last known \($0) min ago" } ?? "no snapshot")
                    .font(style.font(size: 10))
                    .foregroundColor(.secondary)
                    .lineLimit(1)
            } else if !entry.showSections.contains("nodes") {
                Text(entry.state.label)
                    .font(style.font(size: 11, weight: .semibold))
                    .foregroundColor(entry.state.color(style))
                    .lineLimit(1)
            } else if let node = entry.headlineNode, let sample = node.sample {
                HStack(spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
                    Circle()
                        .fill(sample.cpuUsagePct.map { MonitorMeter.color(for: $0, isCPU: true, style: style) }
                              ?? .secondary)
                        .frame(width: 6, height: 6)
                    Text(node.displayLabel)
                        .font(style.font(size: 11, weight: .semibold))
                        .foregroundColor(.primary)
                        .lineLimit(1)
                }
                Text("CPU \(monitorPercent(sample.cpuUsagePct)) · MEM \(monitorPercent(sample.memUsedPct))")
                    .font(style.font(size: 10, weight: .medium))
                    .foregroundColor(.primary)
                    .lineLimit(1)
                    .minimumScaleFactor(0.7)
            } else if let node = entry.headlineNode {
                Text(node.displayLabel)
                    .font(style.font(size: 11, weight: .semibold))
                    .foregroundColor(.primary)
                    .lineLimit(1)
                Text("no sample")
                    .font(style.font(size: 10))
                    .foregroundColor(.secondary)
                    .lineLimit(1)
            } else {
                Text(entry.state.label)
                    .font(style.font(size: 11, weight: .semibold))
                    .foregroundColor(.primary)
                    .lineLimit(1)
                Text("no data")
                    .font(style.font(size: 10))
                    .foregroundColor(.secondary)
                    .lineLimit(1)
            }
            Spacer(minLength: 0)
        }
    }
}
