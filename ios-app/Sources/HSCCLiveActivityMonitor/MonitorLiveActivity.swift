import SwiftUI
import WidgetKit
import ActivityKit

/// HSCCLiveActivityMonitor — the ActivityKit configuration for the NEW HSCC
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
/// Dynamic Island:
///   * compact/minimal — a state dot (serving=ok / unreachable=neutral).
///   * expanded         — headline state + per-node meters + daemon-host line.
///
/// Lock Screen — the full monitor: state header, per-node CPU/RAM/GPU meters
/// (ok/warn/bad colouring), and the daemon-host machine row. Sections honour
/// the settings showSections (state / nodes / host).
struct HSCCLiveActivityMonitor: Widget {
    var body: some WidgetConfiguration {
        let style = WidgetStyle.resolved(for: .liveActivityMonitor)
        return ActivityConfiguration(for: MonitorActivityAttributes.self) { context in
            LockScreenMonitorView(context: context)
                .widgetStyle(style)
                .activityBackgroundTint(style.backgroundColor)
                .activitySystemActionForegroundColor(style.primaryTextColor)
        } dynamicIsland: { context in
            DynamicIsland {
                // Expanded UI.
                DynamicIslandExpandedRegion(.leading) {
                    stateDot(context.state.state, style: style)
                }
                DynamicIslandExpandedRegion(.center) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(headline(context.state.state))
                            .font(style.font(.headline))
                            .foregroundColor(style.primaryTextColor)
                        if let host = context.state.host {
                            Text(hostSummary(host))
                                .font(style.font(.caption))
                                .foregroundColor(style.secondaryTextColor)
                                .lineLimit(1)
                        }
                    }
                }
                DynamicIslandExpandedRegion(.bottom) {
                    nodeMeters(context.state.nodes, style: style)
                }
            } compactLeading: {
                Image(systemName: "gauge.with.dots.needle.bottom.50percent").widgetIcon(style)
                    .foregroundColor(style.warnColor)
            } compactTrailing: {
                stateDot(context.state.state, style: style)
            } minimal: {
                stateDot(context.state.state, style: style)
            }
        }
    }

    // MARK: - Shared rendering helpers

    private func stateDot(_ state: String, style: WidgetStyle) -> some View {
        Circle()
            .fill(stateColor(state, style: style))
            .frame(width: 10, height: 10)
    }

    private func stateColor(_ state: String, style: WidgetStyle) -> Color {
        switch state {
        case "serving": return style.okColor
        case "waking": return style.warnColor
        case "down": return style.badColor
        default: return style.secondaryTextColor   // unreachable / unknown
        }
    }

    private func headline(_ state: String) -> String {
        switch state {
        case "serving": return "Cluster serving"
        case "waking": return "Cluster waking"
        case "down": return "Cluster down"
        case "unreachable": return "Can't reach cluster"
        default: return "HSCC Monitor"
        }
    }

    private func hostSummary(_ host: HostMetric) -> String {
        var parts = [host.os, host.arch].compactMap { $0 }
        if let cpu = host.cpuPct { parts.append("CPU \(fmt(cpu))%") }
        if let mem = host.memPct { parts.append("RAM \(fmt(mem))%") }
        if let disk = host.diskPct { parts.append("Disk \(fmt(disk))%") }
        return parts.joined(separator: " · ")
    }

    /// A compact per-node meter strip for the expanded region.
    private func nodeMeters(_ nodes: [NodeMetric], style: WidgetStyle) -> some View {
        HStack(spacing: 10) {
            ForEach(nodes.prefix(4), id: \.label) { node in
                VStack(alignment: .leading, spacing: 2) {
                    Text(node.label)
                        .font(style.font(size: 10, weight: .semibold))
                        .foregroundColor(style.primaryTextColor)
                    MonitorMiniMeter(label: node.label, cpu: node.cpuPct, mem: node.memPct, gpu: node.gpuUtilPct)
                }
            }
            if nodes.isEmpty {
                Text("No node data")
                    .font(style.font(.caption2))
                    .foregroundColor(style.secondaryTextColor)
            }
            Spacer(minLength: 0)
        }
    }

    private func fmt(_ v: Double) -> String {
        String(format: "%.0f", v)
    }
}

// MARK: - Lock Screen / banner view

/// The Lock Screen (and banner) presentation of the monitor activity — the
/// full live cluster/daemon-host readout. Led by the state headline; then the
/// per-node CPU/RAM/GPU meters (or a muted placeholder if the section is off /
/// empty), then the daemon-host machine row.
struct LockScreenMonitorView: View {
    @Environment(\.widgetStyle) private var style
    let context: ActivityViewContext<MonitorActivityAttributes>

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            // Header: state dot + headline + age.
            HStack(spacing: 8) {
                stateDot(context.state.state, style: style)
                Text(headline(context.state.state))
                    .font(style.font(.headline))
                    .foregroundColor(style.primaryTextColor)
                Spacer()
                Text(ageString(context.state.updatedAt))
                    .font(style.font(.caption2))
                    .foregroundColor(style.secondaryTextColor)
            }
            .widgetHeader(style)

            // Per-node meters.
            if !context.state.nodes.isEmpty {
                nodeMeters
            } else if context.state.host == nil {
                Text("Waiting for first monitor data…")
                    .font(style.font(.caption))
                    .foregroundColor(style.secondaryTextColor)
            }

            // Daemon-host machine row.
            if let host = context.state.host {
                HStack(spacing: 8) {
                    Image(systemName: "desktopcomputer").widgetIcon(style)
                        .foregroundColor(style.secondaryTextColor)
                    Text(hostSummary(host))
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

    private var nodeMeters: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(context.state.nodes.prefix(4), id: \.label) { node in
                MonitorNodeRow(node: node)
            }
        }
    }

    private func stateDot(_ state: String, style: WidgetStyle) -> some View {
        Circle()
            .fill(stateColor(state, style: style))
            .frame(width: 10, height: 10)
    }

    private func stateColor(_ state: String, style: WidgetStyle) -> Color {
        switch state {
        case "serving": return style.okColor
        case "waking": return style.warnColor
        case "down": return style.badColor
        default: return style.secondaryTextColor
        }
    }

    private func headline(_ state: String) -> String {
        switch state {
        case "serving": return "Cluster serving"
        case "waking": return "Cluster waking"
        case "down": return "Cluster down"
        case "unreachable": return "Can't reach cluster"
        default: return "HSCC Monitor"
        }
    }

    private func hostSummary(_ host: HostMetric) -> String {
        var parts = [host.os, host.arch].compactMap { $0 }
        if let cpu = host.cpuPct { parts.append("CPU \(fmt(cpu))%") }
        if let mem = host.memPct { parts.append("RAM \(fmt(mem))%") }
        if let disk = host.diskPct { parts.append("Disk \(fmt(disk))%") }
        return parts.joined(separator: " · ")
    }

    private func ageString(_ date: Date) -> String {
        let total = Int(Date().timeIntervalSince(date))
        if total < 60 { return "now" }
        return "\(total / 60)m ago"
    }

    private func fmt(_ v: Double) -> String {
        String(format: "%.0f", v)
    }
}

// MARK: - Per-node meter row (Lock Screen)

/// One node row: short label + three meters (CPU / RAM / GPU util), each
/// coloured honestly by util (ok <70%, warn 70–90%, bad >90%). A nil metric
/// renders a muted dash rather than a fabricated reading.
struct MonitorNodeRow: View {
    @Environment(\.widgetStyle) private var style
    let node: NodeMetric

    var body: some View {
        HStack(spacing: 8) {
            Text(node.label)
                .font(style.font(size: 11, weight: .semibold))
                .foregroundColor(style.primaryTextColor)
                .frame(width: 40, alignment: .leading)
            MBar(value: node.cpuPct, icon: "cpu")
            MBar(value: node.memPct, icon: "memorychip")
            MBar(value: node.gpuUtilPct, icon: "gpu")
            if let name = node.gpuName, !name.isEmpty {
                Text(name)
                    .font(style.font(.caption2))
                    .foregroundColor(style.secondaryTextColor)
                    .lineLimit(1)
            }
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

// MARK: - Mini meters (Dynamic Island expanded)

/// A compact single-node meter for the expanded Dynamic Island region — keeps
/// the three metrics in the tightest honest strip.
struct MonitorMiniMeter: View {
    @Environment(\.widgetStyle) private var style
    let label: String
    let cpu: Double?
    let mem: Double?
    let gpu: Double?

    var body: some View {
        VStack(alignment: .leading, spacing: 1) {
            meterDot("cpu", cpu)
            meterDot("ram", mem)
            meterDot("gpu", gpu)
        }
    }

    private func meterDot(_ name: String, _ value: Double?) -> some View {
        HStack(spacing: 2) {
            Circle()
                .fill(color(value))
                .frame(width: 5, height: 5)
            Text(fmt(value))
                .font(style.font(size: 8))
                .foregroundColor(style.secondaryTextColor)
        }
        .frame(minWidth: 30, alignment: .leading)
    }

    private func color(_ v: Double?) -> Color {
        guard let v else { return style.secondaryTextColor }
        if v >= 90 { return style.badColor }
        if v >= 70 { return style.warnColor }
        return style.okColor
    }

    private func fmt(_ v: Double?) -> String {
        guard let v else { return "—" }
        return String(format: "%.0f", v)
    }
}
