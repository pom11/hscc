import SwiftUI
import WidgetKit

// ---------------------------------------------------------------------------
// Cluster widget views — small, medium, large, and the three Lock Screen
// accessories.
//
// One `ClusterEntry`, six layouts. The families differ only in how much of the
// entry fits: the accessories get the node gauge, the models count and one line
// of metrics; the small tile adds the topology glyph and the board row; the
// medium adds the metric strip and the idle countdown; the large adds labelled
// topology pairs and a metric card per token. Nothing here invents a number —
// every token renders an entry field or the em dash.
//
// The text rules are in `ClusterMetricText` (pure Foundation, no `Color`) so
// `scripts/cluster_widget_check.sh` can execute them against the committed
// monitor fixture and assert what a Lock Screen line actually says. The heat
// bands are `ClusterMetricHeat`'s (in `ClusterWidget.swift`), mapped to
// `WidgetStyle` here and nowhere else.
// ---------------------------------------------------------------------------

/// The Home Screen cluster widget.
struct ClusterWidget: Widget {
    let kind = "ClusterWidget"

    var body: some WidgetConfiguration {
        let style = WidgetStyle.resolved(for: .widgetCluster)
        return StaticConfiguration(kind: kind, provider: ClusterTimelineProvider()) { entry in
            ClusterWidgetView(entry: entry)
                .widgetStyle(style)
                .containerBackground(for: .widget) {
                    style.backgroundColor
                }
        }
        .configurationDisplayName("Cluster")
        .description("Nodes up, models served, GPU, temperature and load.")
        // t_56b910c8 added the Lock Screen families; the large tile is the
        // full-size layout of the same entry. `accessoryCorner` is not offered:
        // it is the Smart Stack glance surface, and a 4-node topology glyph plus
        // a gauge does not survive that renderer's constraints.
        .supportedFamilies([.systemSmall, .systemMedium, .systemLarge,
                            .accessoryCircular, .accessoryRectangular,
                            .accessoryInline])
    }
}

/// The single view rendered for every widget family, choosing layout by size.
struct ClusterWidgetView: View {
    @Environment(\.widgetStyle) private var style
    let entry: ClusterEntry
    @Environment(\.widgetFamily) private var family

    var body: some View {
        switch family {
        case .systemSmall:
            SmallClusterWidget(entry: entry)
        case .accessoryInline:
            InlineClusterWidget(entry: entry)
        case .accessoryCircular:
            CircularClusterWidget(entry: entry)
        case .accessoryRectangular:
            RectangularClusterWidget(entry: entry)
        case .systemLarge:
            LargeClusterWidget(entry: entry)
        default:
            MediumClusterWidget(entry: entry)
        }
    }
}


extension ClusterMetricHeat {
    /// The one place a heat band becomes a colour.
    func color(_ style: WidgetStyle) -> Color {
        switch self {
        case .calm: return style.okColor
        case .warm: return style.warnColor
        case .hot: return style.badColor
        case .unknown: return style.secondaryTextColor
        }
    }
}

/// One labelled metric token: "GPU 88%", coloured by its own heat band.
private struct MetricToken: View {
    @Environment(\.widgetStyle) private var style
    let text: String
    let heat: ClusterMetricHeat
    var dimmed = false

    var body: some View {
        Text(text)
            .font(style.font(size: 10, weight: .semibold))
            .foregroundColor(dimmed ? style.secondaryTextColor : heat.color(style))
    }
}

// ---------------------------------------------------------------------------
// Small — state + nodes/models + work + topology glyph
// ---------------------------------------------------------------------------

struct SmallClusterWidget: View {
    @Environment(\.widgetStyle) private var style
    let entry: ClusterEntry

    @ViewBuilder
    var body: some View {
        if !entry.configured || entry.paused {
            gateView
        } else {
            VStack(alignment: .leading, spacing: 6) {
                stateLine
                countsLine
                smallWorkLine
                Spacer(minLength: 2)
                MiniTopologyView(pairs: entry.pairs, dimmed: entry.pairsAreStale)
            }
        }
    }

    private var showTopology: Bool { entry.showSections.contains("topology") }
    private var showCounts: Bool { entry.showSections.contains("nodes") || entry.showSections.contains("models") }
    private var showBoard: Bool { entry.showSections.contains("board") }

    private var stateLine: some View {
        HStack(spacing: 5) {
            Circle()
                .fill(style.clusterStateColor(entry.state))
                .frame(width: 8, height: 8)
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

    /// The nodes-up / models-served line (t_56b910c8). Each side is nil-able and
    /// drops out on its own — an unmeasured count is absent, not a dash, because
    /// in a tile this small a dash reads as "zero of something".
    @ViewBuilder
    private var countsLine: some View {
        if showCounts {
            HStack(spacing: 6) {
                if entry.showSections.contains("nodes"), let up = entry.nodesUp, let total = entry.nodesTotal {
                    Text("\(up)/\(total) up")
                        .font(style.font(.caption2, weight: .semibold))
                        .foregroundColor(style.primaryTextColor)
                }
                if entry.showSections.contains("models"), let models = entry.modelCount {
                    Text(ClusterMetricText.models(models))
                        .font(style.font(.caption2))
                        .foregroundColor(style.secondaryTextColor)
                }
                Spacer(minLength: 0)
            }
            metricLine
        }
    }

    /// The GPU / temperature / load line, live-only (the store holds no metric
    /// projection, so a failed monitor read shows no metric line at all).
    @ViewBuilder
    private var metricLine: some View {
        let parts = ClusterMetricText.metricParts(gpuUtilPct: entry.gpuUtilPct,
                                                  gpuTempC: entry.gpuTempC,
                                                  cpuTempC: entry.cpuTempC,
                                                  load1m: entry.load1m)
        if entry.showSections.contains("nodes"), !parts.isEmpty {
            Text(parts.joined(separator: " · "))
                .font(style.font(size: 9, weight: .medium))
                .foregroundColor(style.primaryTextColor)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
        }
    }

    /// Compact one-line "what the fleet is DOING" strip for the small widget:
    /// running + queue counts, with a red warning marker when there are blocked
    /// cards. Omitted entirely when no kanban data is present, so the small
    /// widget still fits its state + topology in a tight space.
    @ViewBuilder
    private var smallWorkLine: some View {
        let hasWork = entry.runningCards != nil || entry.queueDepth != nil || (entry.blockedCards ?? 0) > 0
        if showBoard && hasWork {
            HStack(spacing: 10) {
                if let running = entry.runningCards {
                    HStack(spacing: 3) {
                        Image(systemName: "play.fill").widgetIcon(style)
                            .font(style.font(size: 7))
                            .foregroundColor(style.okColor)
                        Text("\(running)")
                            .font(style.font(.caption2, weight: .semibold))
                            .foregroundColor(style.primaryTextColor)
                    }
                }
                if let queue = entry.queueDepth {
                    HStack(spacing: 3) {
                        Image(systemName: "square.stack.3d.up").widgetIcon(style)
                            .font(style.font(size: 7))
                            .foregroundColor(style.secondaryTextColor)
                        Text("\(queue)")
                            .font(style.font(.caption2, weight: .semibold))
                            .foregroundColor(style.primaryTextColor)
                    }
                }
                Spacer(minLength: 0)
                if let blocked = entry.blockedCards, blocked > 0 {
                    Image(systemName: "exclamationmark.triangle.fill").widgetIcon(style)
                        .font(style.font(size: 8))
                        .foregroundColor(style.badColor)
                }
            }
        }
    }

    private var gateView: some View {
        VStack(alignment: .leading, spacing: 6) {
            Image(systemName: entry.paused ? "pause.circle" : "gearshape").widgetIcon(style)
                .font(style.font(.title3))
                .foregroundColor(style.secondaryTextColor)
            Text(entry.paused ? "Paused in Settings" : "Set up the app to see the cluster")
                .font(style.font(.caption2))
                .foregroundColor(style.secondaryTextColor)
        }
    }
}

// ---------------------------------------------------------------------------
// Medium — topology pairs + models + nodes + metrics + idle remaining
// ---------------------------------------------------------------------------

struct MediumClusterWidget: View {
    @Environment(\.widgetStyle) private var style
    let entry: ClusterEntry

    @ViewBuilder
    var body: some View {
        if !entry.configured || entry.paused {
            gateView
        } else {
            VStack(alignment: .leading, spacing: 8) {
                // State + the counts that matter at a glance.
                HStack(spacing: 5) {
                    Circle()
                        .fill(style.clusterStateColor(entry.state))
                        .frame(width: 8, height: 8)
                    Text(entry.state.label)
                        .font(style.font(.headline))
                        .foregroundColor(style.primaryTextColor)
                    Spacer()
                    if entry.showSections.contains("nodes"),
                       let up = entry.nodesUp, let total = entry.nodesTotal {
                        metricLabel(value: "\(up)/\(total)", label: "nodes up")
                    }
                    if entry.showSections.contains("models"), let models = entry.modelCount {
                        metricLabel(value: ClusterMetricText.models(models), label: "served")
                    }
                    if let idle = entry.idleMinutesRemaining {
                        metricLabel(value: "\(idle)m", label: "to autodown")
                    }
                }
                .widgetHeader(style)
                // GPU / temperature / load, each in its own heat band.
                metricStrip
                // The topology pairs with per-node colour.
                HStack(spacing: 16) {
                    if entry.showSections.contains("topology") {
                        ForEach(entry.pairs) { pair in
                            miniPair(pair, dimmed: entry.pairsAreStale)
                        }
                    }
                    Spacer(minLength: 0)
                }
                // What the fleet is DOING: running cards, queue depth, and a
                // failure indicator (blocked cards needing attention).
                workMetricsRow
                // The one honest sentence about a failed read, when there is one.
                if let failure = entry.failure {
                    Text(failure.operatorLine)
                        .font(style.font(.caption2))
                        .foregroundColor(style.secondaryTextColor)
                        .lineLimit(2)
                }
            }
        }
    }

    /// The GPU / temperature / load row (t_56b910c8). Sits above the topology so
    /// a hot GPU is the second thing the eye reaches, and disappears entirely
    /// when the monitor gave nothing — never a row of dashes.
    @ViewBuilder
    private var metricStrip: some View {
        let tokens = ClusterMetricText.tokens(gpuUtilPct: entry.gpuUtilPct,
                                              gpuTempC: entry.gpuTempC,
                                              cpuTempC: entry.cpuTempC,
                                              load1m: entry.load1m)
        if entry.showSections.contains("nodes"), !tokens.isEmpty {
            HStack(spacing: 10) {
                ForEach(Array(tokens.enumerated()), id: \.offset) { _, token in
                    MetricToken(text: token.label, heat: token.heat, dimmed: entry.isStale)
                }
                Spacer(minLength: 0)
            }
        }
    }

    /// The board work row: running cards + queue depth as neutral metrics, and
    /// a red failure badge when blocked cards need attention. Each count is
    /// optional — a failed kanban fetch simply omits that metric.
    @ViewBuilder
    private var workMetricsRow: some View {
        if entry.showSections.contains("board") {
            HStack(spacing: 12) {
                if let running = entry.runningCards {
                    HStack(spacing: 4) {
                        Image(systemName: "play.circle.fill").widgetIcon(style)
                            .font(style.font(size: 10))
                            .foregroundColor(style.okColor)
                        metricLabel(value: "\(running)", label: "running")
                    }
                }
                if let queue = entry.queueDepth {
                    HStack(spacing: 4) {
                        Image(systemName: "square.stack.3d.up").widgetIcon(style)
                            .font(style.font(size: 10))
                            .foregroundColor(style.secondaryTextColor)
                        metricLabel(value: "\(queue)", label: "queued")
                    }
                }
                Spacer(minLength: 0)
                if let blocked = entry.blockedCards, blocked > 0 {
                    failureBadge(count: blocked)
                }
            }
        }
    }

    /// The failure indicator — a red badge the operator can see at a glance.
    private func failureBadge(count: Int) -> some View {
        HStack(spacing: 3) {
            Image(systemName: "exclamationmark.triangle.fill").widgetIcon(style)
                .font(style.font(size: 10))
                .foregroundColor(style.badColor)
            Text("\(count)")
                .font(style.font(.caption, weight: .semibold))
                .foregroundColor(style.badColor)
        }
        .padding(.horizontal, 6)
        .padding(.vertical, 2)
        .background(Capsule().fill(style.badColor.opacity(0.14)))
    }

    private func miniPair(_ pair: TopologyPair, dimmed: Bool = false) -> some View {
        HStack(spacing: 4) {
            dot(pair.nodes[0], dimmed: dimmed)
            Rectangle()
                .fill(style.secondaryTextColor.opacity(0.5))
                .frame(width: 12, height: 2)
            dot(pair.nodes[1], dimmed: dimmed)
        }
        .overlay(alignment: .bottom) {
            Text(pair.role)
                .font(style.font(size: 9))
                .foregroundColor(style.secondaryTextColor)
                .offset(y: 12)
        }
        .padding(.bottom, 6)
    }

    private func dot(_ node: TopologyNode, dimmed: Bool = false) -> some View {
        HStack(spacing: 3) {
            Circle()
                .fill(dimmed ? style.secondaryTextColor.opacity(0.55) : style.nodeStateColor(node.state))
                .frame(width: 8, height: 8)
            Text(node.label)
                .font(style.font(size: 10, weight: .semibold))
                .foregroundColor(dimmed ? style.secondaryTextColor : style.primaryTextColor)
        }
    }

    private func metricLabel(value: String, label: String) -> some View {
        VStack(spacing: 0) {
            Text(value)
                .font(style.font(.caption, weight: .semibold))
                .foregroundColor(style.primaryTextColor)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
            Text(label)
                .font(style.font(size: 9))
                .foregroundColor(style.secondaryTextColor)
        }
    }

    private var gateView: some View {
        VStack(alignment: .leading, spacing: 6) {
            Image(systemName: entry.paused ? "pause.circle" : "gearshape").widgetIcon(style)
                .font(style.font(.title3))
                .foregroundColor(style.secondaryTextColor)
            Text(entry.paused ? "Paused in Settings" : "Set up the app to see the cluster")
                .font(style.font(.caption2))
                .foregroundColor(style.secondaryTextColor)
        }
    }
}

// ---------------------------------------------------------------------------
// Large — state + counts, every metric, labelled topology pairs, board work
// ---------------------------------------------------------------------------

struct LargeClusterWidget: View {
    @Environment(\.widgetStyle) private var style
    let entry: ClusterEntry

    @ViewBuilder
    var body: some View {
        if !entry.configured || entry.paused {
            gateView
        } else {
            VStack(alignment: .leading, spacing: 10) {
                stateLine
                summaryRow
                metricStrip
                topologySection
                workRow
                if let failure = entry.failure {
                    Text(failure.operatorLine)
                        .font(style.font(.caption2))
                        .foregroundColor(style.secondaryTextColor)
                        .lineLimit(2)
                }
                Spacer(minLength: 0)
            }
        }
    }

    private var showNodes: Bool { entry.showSections.contains("nodes") }
    private var showModels: Bool { entry.showSections.contains("models") }
    private var showTopology: Bool { entry.showSections.contains("topology") }
    private var showBoard: Bool { entry.showSections.contains("board") }

    private var stateLine: some View {
        HStack(spacing: 6) {
            Circle()
                .fill(style.clusterStateColor(entry.state))
                .frame(width: 10, height: 10)
            Text(entry.state.label)
                .font(style.font(.title3, weight: .semibold))
                .foregroundColor(style.primaryTextColor)
                .lineLimit(1)
                .minimumScaleFactor(0.8)
            Spacer(minLength: 0)
            if let age = entry.lastKnownAgeMinutes {
                Text("last known \(age)m ago")
                    .font(style.font(.caption))
                    .foregroundColor(style.secondaryTextColor)
            }
        }
    .widgetHeader(style)
    }

    /// Nodes / models / idle, each present only when the entry actually carries
    /// it and the operator left the section on — an absent count is not a dash.
    @ViewBuilder
    private var summaryRow: some View {
        HStack(spacing: 20) {
            if showNodes, let up = entry.nodesUp, let total = entry.nodesTotal {
                metricLabel(value: "\(up)/\(total)", label: "nodes up")
            }
            if showModels, let models = entry.modelCount {
                metricLabel(value: ClusterMetricText.models(models), label: "served")
            }
            if let idle = entry.idleMinutesRemaining {
                metricLabel(value: "\(idle)m", label: "to autodown")
            }
            Spacer(minLength: 0)
        }
    }

    /// The GPU / temperature / load strip, one card per reported token. A nil
    /// metric drops its whole card (never a dash), and a stale tile dims them
    /// so old numbers cannot read as live.
    @ViewBuilder
    private var metricStrip: some View {
        let tokens = ClusterMetricText.tokens(gpuUtilPct: entry.gpuUtilPct,
                                              gpuTempC: entry.gpuTempC,
                                              cpuTempC: entry.cpuTempC,
                                              load1m: entry.load1m)
        if showNodes, !tokens.isEmpty {
            HStack(spacing: 8) {
                ForEach(Array(tokens.enumerated()), id: \.offset) { _, token in
                    Text(token.label)
                        .font(style.font(size: 13, weight: .semibold))
                        .foregroundColor(entry.isStale ? style.secondaryTextColor : token.heat.color(style))
                        .lineLimit(1)
                        .minimumScaleFactor(0.6)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 8)
                        .background(RoundedRectangle(cornerRadius: 8)
                            .fill(style.raisedColor))
                }
            }
        }
    }

    /// Each TP pair on its own row with the role named and both node labels —
    /// the extra width the large family buys over the small glyph.
    @ViewBuilder
    private var topologySection: some View {
        if showTopology, !entry.pairs.isEmpty {
            VStack(alignment: .leading, spacing: 6) {
                ForEach(entry.pairs) { pair in
                    HStack(spacing: 8) {
                        Text(pair.role)
                            .font(style.font(size: 11, weight: .medium))
                            .foregroundColor(style.secondaryTextColor)
                            .frame(width: 88, alignment: .leading)
                        ForEach(pair.nodes) { node in
                            nodeChip(node)
                        }
                        Spacer(minLength: 0)
                    }
                }
            }
        }
    }

    private func nodeChip(_ node: TopologyNode) -> some View {
        HStack(spacing: 4) {
            Circle()
                .fill(entry.pairsAreStale ? style.secondaryTextColor.opacity(0.55) : style.nodeStateColor(node.state))
                .frame(width: 8, height: 8)
            Text(node.label)
                .font(style.font(size: 12, weight: .semibold))
                .foregroundColor(entry.pairsAreStale ? style.secondaryTextColor : style.primaryTextColor)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 4)
        .background(RoundedRectangle(cornerRadius: 6).fill(style.raisedColor))
    }

    /// Running / queued / blocked, each optional independently.
    @ViewBuilder
    private var workRow: some View {
        let hasWork = entry.runningCards != nil || entry.queueDepth != nil || (entry.blockedCards ?? 0) > 0
        if showBoard && hasWork {
            HStack(spacing: 20) {
                if let running = entry.runningCards {
                    workMetric(icon: "play.fill", tint: style.okColor,
                               value: running, label: "running")
                }
                if let queue = entry.queueDepth {
                    workMetric(icon: "square.stack.3d.up", tint: style.secondaryTextColor,
                               value: queue, label: "queued")
                }
                if let blocked = entry.blockedCards, blocked > 0 {
                    workMetric(icon: "exclamationmark.triangle.fill", tint: style.badColor,
                               value: blocked, label: "blocked")
                }
                Spacer(minLength: 0)
            }
        }
    }

    private func workMetric(icon: String, tint: Color, value: Int, label: String) -> some View {
        HStack(spacing: 6) {
            Image(systemName: icon).widgetIcon(style)
                .font(style.font(size: 11))
                .foregroundColor(tint)
            metricLabel(value: "\(value)", label: label)
        }
    }

    private func metricLabel(value: String, label: String) -> some View {
        VStack(spacing: 1) {
            Text(value)
                .font(style.font(.headline))
                .foregroundColor(style.primaryTextColor)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
            Text(label)
                .font(style.font(size: 10))
                .foregroundColor(style.secondaryTextColor)
        }
    }

    private var gateView: some View {
        VStack(alignment: .leading, spacing: 6) {
            Image(systemName: entry.paused ? "pause.circle" : "gearshape").widgetIcon(style)
                .font(style.font(.title3))
                .foregroundColor(style.secondaryTextColor)
            Text(entry.paused ? "Paused in Settings" : "Set up the app to see the cluster")
                .font(style.font(.caption2))
                .foregroundColor(style.secondaryTextColor)
        }
    }
}

// ---------------------------------------------------------------------------
// Lock Screen accessories (t_56b910c8)
//
// Space is the design constraint: one line inline, a gauge circular, two lines
// rectangular. All three share the rule that live data wins and the gate label
// (which carries `WidgetDataFailure.shortLabel`) only appears when the entry has
// nothing live to draw — a tile that lost ONE read keeps showing the rest.
// ---------------------------------------------------------------------------

/// The Lock Screen circular: nodes up over nodes listed, as a capacity gauge.
/// The gauge is chosen over a plain number because the denominator is the point
/// — "3/4" reads as a degraded fleet, "3" does not.
struct CircularClusterWidget: View {
    @Environment(\.widgetStyle) private var style
    let entry: ClusterEntry

    var body: some View {
        if !entry.configured || entry.paused {
            Text(entry.shortGateLabel)
                .font(style.font(size: 9, weight: .semibold))
                .foregroundColor(.secondary)
                .minimumScaleFactor(0.6)
                .accessibilityLabel(entry.shortGateLabel)
        } else if let up = entry.nodesUp, let total = entry.nodesTotal, total > 0 {
            Gauge(value: Double(min(max(up, 0), total)), in: 0...Double(total)) {
                Text("up")
            } currentValueLabel: {
                Text("\(up)/\(total)")
                    .font(style.font(size: 11, weight: .semibold))
            }
            .gaugeStyle(.accessoryCircularCapacity)
            .tint(.primary)
            .accessibilityLabel("\(up) of \(total) nodes up, \(entry.state.label)")
        } else if entry.state != .unknown {
            // No host list (the monitor degraded or the fleet is off) but the
            // state read answered: say the state, which IS the fact.
            Text(entry.state.label)
                .font(style.font(size: 10, weight: .semibold))
                .foregroundColor(.primary)
                .minimumScaleFactor(0.6)
                .multilineTextAlignment(.center)
                .accessibilityLabel(entry.state.label)
        } else {
            Text(entry.shortGateLabel)
                .font(style.font(size: 9, weight: .semibold))
                .foregroundColor(.secondary)
                .minimumScaleFactor(0.6)
                .accessibilityLabel(entry.shortGateLabel)
        }
    }
}

/// The Lock Screen rectangular: state + nodes + models on line one, the GPU /
/// temperature / load strip on line two.
struct RectangularClusterWidget: View {
    @Environment(\.widgetStyle) private var style
    let entry: ClusterEntry

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            if !entry.configured || entry.paused {
                Text(entry.shortGateLabel)
                    .font(style.font(size: 11, weight: .semibold))
                    .foregroundColor(.secondary)
                    .lineLimit(1)
                Text("Open HSCC to configure the cluster")
                    .font(style.font(size: 9))
                    .foregroundColor(.secondary)
                    .lineLimit(1)
            } else {
                HStack(spacing: 4) {
                    Circle()
                        .fill(.primary)
                        .frame(width: 6, height: 6)
                    Text(lineOne).widgetHeader(style)
                        .font(style.font(size: 11, weight: .semibold))
                        .foregroundColor(.primary)
                        .lineLimit(1)
                        .minimumScaleFactor(0.7)
                }
                .accessibilityLabel(lineOne)
                Text(lineTwo)
                    .font(style.font(size: 10, weight: .medium))
                    .foregroundColor(lineTwoColor)
                    .lineLimit(1)
                    .minimumScaleFactor(0.7)
                    .accessibilityLabel(lineTwo)
            }
            Spacer(minLength: 0)
        }
    }

    /// "Serving · 3/4 up · 2 models" — whatever the entry actually has.
    private var lineOne: String {
        var parts: [String] = []
        if entry.state != .unknown, entry.showSections.contains("state") { parts.append(entry.state.label) }
        if entry.showSections.contains("nodes") {
            let nodeText = ClusterMetricText.nodes(up: entry.nodesUp, total: entry.nodesTotal)
            if nodeText != ClusterMetricText.dash { parts.append(nodeText + " up") }
        }
        if entry.showSections.contains("models"), let models = entry.modelCount {
            parts.append(ClusterMetricText.models(models))
        }
        return parts.isEmpty ? entry.shortGateLabel : parts.joined(separator: " · ")
    }

    /// The metric strip, or the failure's one-line explanation.
    private var lineTwo: String {
        let parts = ClusterMetricText.metricParts(gpuUtilPct: entry.gpuUtilPct,
                                                  gpuTempC: entry.gpuTempC,
                                                  cpuTempC: entry.cpuTempC,
                                                  load1m: entry.load1m)
        if !parts.isEmpty, entry.showSections.contains("nodes") { return parts.joined(separator: " · ") }
        if let age = entry.lastKnownAgeMinutes { return "last known \(age) min ago" }
        return entry.failure?.shortLabel ?? ClusterMetricText.dash
    }

    private var lineTwoColor: Color {
        ClusterMetricHeat.percent(entry.gpuUtilPct).color(style)
    }
}

/// The Lock Screen inline: one line, text only — nodes up, models served, GPU.
struct InlineClusterWidget: View {
    @Environment(\.widgetStyle) private var style
    let entry: ClusterEntry

    var body: some View {
        Text(ClusterMetricText.inlineLine(nodesUp: entry.nodesUp,
                                         nodesTotal: entry.nodesTotal,
                                         models: entry.showSections.contains("models") ? entry.modelCount : nil,
                                         gpuUtilPct: entry.showSections.contains("nodes") ? entry.gpuUtilPct : nil,
                                         gateLabel: entry.shortGateLabel))
            .accessibilityLabel(ClusterMetricText.inlineLine(nodesUp: entry.nodesUp,
                                                            nodesTotal: entry.nodesTotal,
                                                            models: entry.modelCount,
                                                            gpuUtilPct: entry.gpuUtilPct,
                                                            gateLabel: entry.shortGateLabel))
            .font(style.font(.caption))
    }
}

// ---------------------------------------------------------------------------
// Compact topology glyph
// ---------------------------------------------------------------------------

/// The compact topology glyph — the four nodes as two dotted TP pairs. Used by
/// the small widget and (with `dimmed`) the unreachable small widget so a
/// stale topology is clearly not-live.
struct MiniTopologyView: View {
    @Environment(\.widgetStyle) private var style
    let pairs: [TopologyPair]
    /// When true, render the topology in a muted state (the data is stale —
    /// these dots came out of the last-known snapshot, not this refresh).
    var dimmed = false

    var body: some View {
        HStack(spacing: 14) {
            ForEach(pairs) { pair in
                HStack(spacing: 4) {
                    dot(pair.nodes[0])
                    Rectangle()
                        .fill(style.secondaryTextColor.opacity(0.4))
                        .frame(width: 8, height: 2)
                    dot(pair.nodes[1])
                }
            }
            Spacer(minLength: 0)
        }
    }

    private func dot(_ node: TopologyNode) -> some View {
        Circle()
            .fill(dimmed ? style.secondaryTextColor.opacity(0.55) : style.nodeStateColor(node.state))
            .frame(width: 7, height: 7)
    }
}
