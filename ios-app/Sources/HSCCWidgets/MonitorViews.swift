import SwiftUI
import WidgetKit

// ---------------------------------------------------------------------------
// HSCC Monitor widget views — medium + large.
// ---------------------------------------------------------------------------

/// The Home Screen "HSCC Monitor" widget: `hscc cluster monitor` per-node
/// metrics + the daemon-host machine data, refreshed on the configured cadence.
struct MonitorWidget: Widget {
    let kind = "MonitorWidget"

    var body: some WidgetConfiguration {
        StaticConfiguration(kind: kind, provider: MonitorTimelineProvider()) { entry in
            MonitorWidgetView(entry: entry)
                .containerBackground(for: .widget) {
                    Theme.Semantic.surface
                }
        }
        .configurationDisplayName("HSCC Monitor")
        .description("Cluster monitor metrics and the machine HSCC runs on.")
        .supportedFamilies([.systemMedium, .systemLarge])
    }
}

/// The single view rendered for every family, choosing layout by size.
struct MonitorWidgetView: View {
    let entry: MonitorEntry
    @Environment(\.widgetFamily) private var family

    var body: some View {
        switch family {
        case .systemLarge:
            LargeMonitorWidget(entry: entry)
        default:
            MediumMonitorWidget(entry: entry)
        }
    }
}

// ---------------------------------------------------------------------------
// Shared building blocks
// ---------------------------------------------------------------------------

/// Honest rendition of the gate states (unconfigured / paused / unreachable).
/// `entry.isLive` distinguishes live content from a full-pane gate. The
/// unreachable pane carries the last-known snapshot (if any) dimmed with its
/// age — a stale read is never presented as live data.
extension MonitorEntry {
    var isLive: Bool { configured && !paused && state != .unreachable }

    @ViewBuilder
    var gateView: some View {
        if !configured {
            MonitorGateView(icon: "gearshape",
                            title: "Set up the app to see the cluster",
                            caption: "Add host, port, and token in Settings.")
        } else if paused {
            MonitorGateView(icon: "pause.circle",
                            title: "Paused in Settings",
                            caption: "Turn this widget back on in Settings.")
        } else if state == .unreachable {
            unreachablePane
        } else {
            EmptyView()
        }
    }

    /// The unreachable pane — the last-known snapshot dimmed for stale + age.
    @ViewBuilder
    private var unreachablePane: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.sm.rawValue) {
            HStack(spacing: Theme.Spacing.xs.rawValue) {
                Circle().fill(state.color).frame(width: 8, height: 8)
                Text(state.label)
                    .font(.subheadline.weight(.semibold))
                    .foregroundColor(Theme.Semantic.onSurface)
                Spacer()
            }
            if !nodes.isEmpty {
                Text("showing last-known data")
                    .font(.caption2)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
            if let age = lastKnownAgeMinutes {
                Text("\(age) min ago")
                    .font(.caption2)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// The full-pane unconfigured / paused gate.
struct MonitorGateView: View {
    let icon: String
    let title: String
    let caption: String

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.sm.rawValue) {
            Image(systemName: icon)
                .font(.title3)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
            Text(title)
                .font(.caption)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
            Text(caption)
                .font(.caption2)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// A single labelled meter: label left, thin bar, percent right. The bar colour
/// is honest — derived from the value against warn/bad thresholds (CPU>85 warn,
/// >95 bad; other gauges at >90 warn, >95 bad). Dims for stale last-known data.
struct MonitorMeter: View {
    let label: String
    let value: Double?
    /// True for a CPU meter (uses the 85 warn threshold; others 90).
    var isCPU = false
    var dimmed = false

    var body: some View {
        HStack(spacing: Theme.Spacing.sm.rawValue) {
            Text(label)
                .font(.system(size: 10))
                .foregroundColor(dimmed ? Theme.Semantic.onSurfaceMuted : Theme.Semantic.onSurface)
                .frame(width: 30, alignment: .leading)
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule()
                        .fill(dimmed ? Theme.Semantic.neutral.opacity(0.22) : Theme.Semantic.neutral.opacity(0.22))
                    if let value {
                        Capsule()
                            .fill(dimmed ? Theme.Semantic.neutral.opacity(0.5) : Self.color(for: value, isCPU: isCPU))
                            .frame(width: max(geo.size.width * CGFloat(min(max(value / 100, 0), 1)), 3))
                    }
                }
            }
            .frame(height: 6)
            Text(valueText)
                .font(.system(size: 10, design: .monospaced))
                .foregroundColor(dimmed ? Theme.Semantic.onSurfaceMuted : Theme.Semantic.onSurface)
                .frame(width: 30, alignment: .trailing)
        }
    }

    private var valueText: String {
        guard let value else { return "—" }
        return "\(Int(value.rounded()))%"
    }

    /// Honest data-colouring: CPU>85 warn, >95 bad; other gauges >90 warn, >95 bad.
    static func color(for value: Double, isCPU: Bool) -> Color {
        let warn = isCPU ? 85.0 : 90.0
        let bad = 95.0
        if value > bad { return Theme.Semantic.bad }
        if value > warn { return Theme.Semantic.warn }
        return Theme.Semantic.ok
    }
}

/// Displayed node label — the short tail of the node IP, or the sample hostname.
extension MonitorNode {
    var displayLabel: String {
        if let hostname = sample?.hostname, !hostname.isEmpty { return hostname }
        let parts = host.split(separator: ".")
        return parts.count > 1 ? "." + parts.suffix(1).joined() : host
    }
}

// ---------------------------------------------------------------------------
// Medium — state line + a compact 2×2 node grid + a slim daemon-host strip
// ---------------------------------------------------------------------------

struct MediumMonitorWidget: View {
    let entry: MonitorEntry

    var body: some View {
        if entry.isLive {
            VStack(alignment: .leading, spacing: Theme.Spacing.sm.rawValue) {
                stateLine
                if showNodes && !entry.nodes.isEmpty {
                    nodeGrid
                }
                if showHost, let host = entry.daemonHost {
                    HostStrip(host: host)
                }
            }
        } else {
            entry.gateView
        }
    }

    private var showNodes: Bool { entry.showSections.contains("nodes") }
    private var showHost: Bool { entry.showSections.contains("host") }

    private var stateLine: some View {
        HStack(spacing: Theme.Spacing.xs.rawValue) {
            Circle().fill(entry.state.color).frame(width: 8, height: 8)
            Text(entry.state.label)
                .font(.headline)
                .foregroundColor(Theme.Semantic.onSurface)
            Spacer()
            if let age = entry.lastKnownAgeMinutes {
                Text("\(age)m")
                    .font(.caption2)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
        }
    }

    /// 2×2 compact node grid — each cell: host label + CPU/RAM/GPU bars.
    private var nodeGrid: some View {
        LazyVGrid(columns: [GridItem(.flexible(), spacing: Theme.Spacing.md.rawValue),
                            GridItem(.flexible(), spacing: Theme.Spacing.md.rawValue)],
                  spacing: Theme.Spacing.sm.rawValue) {
            ForEach(entry.nodes) { node in
                MediumNodeCell(node: node)
            }
        }
    }
}

/// A compact node cell for the medium grid: host label + three tiny bars.
struct MediumNodeCell: View {
    let node: MonitorNode
    private var sample: MonitorSample? { node.sample }

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
            nodeHeader
            if let sample {
                MonitorMeter(label: "CPU", value: sample.cpuUsagePct, isCPU: true)
                MonitorMeter(label: "RAM", value: sample.memUsedPct)
                MonitorMeter(label: "GPU", value: sample.gpuUtilPct)
            } else if let error = node.error {
                Text(error).font(.caption2).foregroundColor(Theme.Semantic.bad).lineLimit(2)
            } else {
                Text("no sample").font(.caption2).foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
        }
    }

    private var nodeHeader: some View {
        HStack(spacing: Theme.Spacing.xxs.rawValue) {
            Circle()
                .fill(node.sample?.cpuUsagePct.map { MonitorMeter.color(for: $0, isCPU: true) } ?? Theme.Semantic.neutral)
                .frame(width: 6, height: 6)
            Text(node.displayLabel)
                .font(.hsccMono(10, weight: .semibold))
                .foregroundColor(Theme.Semantic.onSurface)
                .lineLimit(1)
        }
    }
}

/// A slim daemon-host strip for the medium widget: hostname + CPU/RAM/DISK bars.
struct HostStrip: View {
    let host: DaemonHostResponse
    var dimmed = false

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs.rawValue) {
            HStack(spacing: Theme.Spacing.xs.rawValue) {
                Image(systemName: "laptopcomputer")
                    .font(.system(size: 10))
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
                Text(hostTitle)
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundColor(Theme.Semantic.onSurface)
                    .lineLimit(1)
                Spacer()
                if host.daemon_running == true {
                    Circle().fill(Theme.Semantic.ok).frame(width: 5, height: 5)
                }
            }
            HStack(spacing: Theme.Spacing.md.rawValue) {
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
    let entry: MonitorEntry

    var body: some View {
        if entry.isLive {
            VStack(alignment: .leading, spacing: Theme.Spacing.sm.rawValue) {
                stateLine
                if showNodes && !entry.nodes.isEmpty {
                    nodesTable
                }
                if showHost, let host = entry.daemonHost {
                    HostBlock(host: host)
                }
                Spacer(minLength: 0)
            }
        } else {
            entry.gateView
        }
    }

    private var showNodes: Bool { entry.showSections.contains("nodes") }
    private var showHost: Bool { entry.showSections.contains("host") }

    private var stateLine: some View {
        HStack(spacing: Theme.Spacing.xs.rawValue) {
            Circle().fill(entry.state.color).frame(width: 8, height: 8)
            Text(entry.state.label)
                .font(.headline)
                .foregroundColor(Theme.Semantic.onSurface)
            Spacer()
            if let age = entry.lastKnownAgeMinutes {
                Text("\(age) min ago")
                    .font(.caption2)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
        }
    }

    private var nodesTable: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs.rawValue) {
            ForEach(entry.nodes) { node in
                LargeNodeRow(node: node)
            }
        }
    }
}

/// A full per-node row for the large widget: label + CPU/GPU/RAM meters.
struct LargeNodeRow: View {
    let node: MonitorNode
    private var sample: MonitorSample? { node.sample }

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
            header
            if let sample {
                MonitorMeter(label: "CPU", value: sample.cpuUsagePct, isCPU: true)
                MonitorMeter(label: "GPU", value: sample.gpuUtilPct)
                MonitorMeter(label: "RAM", value: sample.memUsedPct)
            } else if let error = node.error {
                Text(error)
                    .font(.caption2)
                    .foregroundColor(Theme.Semantic.bad)
                    .lineLimit(2)
            } else {
                Text("no sample")
                    .font(.caption2)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
        }
    }

    private var header: some View {
        HStack(spacing: Theme.Spacing.xs.rawValue) {
            Circle()
                .fill((sample?.cpuUsagePct).map { MonitorMeter.color(for: $0, isCPU: true) } ?? Theme.Semantic.neutral)
                .frame(width: 6, height: 6)
            Text(node.displayLabel)
                .font(.hsccMono(11, weight: .semibold))
                .foregroundColor(Theme.Semantic.onSurface)
            Spacer()
            if let u = sample?.uptimeText {
                Text(u)
                    .font(.hsccMono(9))
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
        }
    }
}

/// The full daemon-host block for the large widget: hostname line + gauges.
struct HostBlock: View {
    let host: DaemonHostResponse
    var dimmed = false

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs.rawValue) {
            HStack(spacing: Theme.Spacing.xs.rawValue) {
                Image(systemName: "laptopcomputer")
                    .font(.system(size: 11))
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
                Text(hostTitle)
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundColor(Theme.Semantic.onSurface)
                    .lineLimit(1)
                Spacer()
                daemonStatus
            }
            MonitorMeter(label: "CPU", value: host.cpuPercent, isCPU: true)
            MonitorMeter(label: "RAM", value: host.memPercent)
            MonitorMeter(label: "DISK", value: host.diskPercent)
            if let line = processLine {
                Text(line)
                    .font(.hsccMono(9))
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
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
                Circle().fill(Theme.Semantic.ok).frame(width: 5, height: 5)
                Text("daemon up")
                    .font(.system(size: 9))
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
        } else {
            Text("daemon down")
                .font(.system(size: 9))
                .foregroundColor(Theme.Semantic.bad)
        }
    }
}
