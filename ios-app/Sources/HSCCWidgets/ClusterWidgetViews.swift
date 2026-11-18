import SwiftUI
import WidgetKit

// ---------------------------------------------------------------------------
// Cluster widget views — small, medium, large, and the three Lock Screen
// accessories.
//
// One `ClusterEntry`, six layouts. The families differ only in how much of the
// entry fits: the accessories get the node gauge, the models count and one line
// of metrics; the small tile adds the topology glyph and the board row; the
// medium adds the metric strip and the idle countdown; the large tile gives
// every one of those a row of its own instead of a strip. Nothing here invents
// a number — every token renders an entry field or the em dash.
//
// The text rules are in `ClusterMetricText` (pure Foundation, no `Color`) so
// `scripts/cluster_widget_check.sh` can execute them against the committed
// monitor fixture and assert what a Lock Screen line actually says. The heat
// bands are `ClusterMetricHeat`'s (in `ClusterWidgetMetrics.swift`, where they
// moved with the projection), mapped to `Theme.Semantic` here and nowhere else.
// ---------------------------------------------------------------------------

/// The Home Screen cluster widget.
struct ClusterWidget: Widget {
    let kind = "ClusterWidget"

    var body: some WidgetConfiguration {
        StaticConfiguration(kind: kind, provider: ClusterTimelineProvider()) { entry in
            ClusterWidgetView(entry: entry)
                .containerBackground(for: .widget) {
                    Theme.Semantic.surface
                }
        }
        .configurationDisplayName("Cluster")
        .description("Nodes up, models served, GPU, temperature and load.")
        // Every size this entry can fill. t_56b910c8 added the Lock Screen
        // families; t_b3ae3809 adds `systemLarge`, which the dispatch below had
        // been swallowing in its `default:` arm (so a large tile, had it ever
        // been offered, would have drawn the medium layout). `accessoryCorner`
        // is not offered: it is the Smart Stack glance surface, and a 4-node
        // topology glyph plus a gauge does not survive that renderer's
        // constraints.
        .supportedFamilies([.systemSmall, .systemMedium, .systemLarge,
                            .accessoryCircular, .accessoryRectangular,
                            .accessoryInline])
    }
}

/// The single view rendered for every widget family, choosing layout by size.
struct ClusterWidgetView: View {
    let entry: ClusterEntry
    @Environment(\.widgetFamily) private var family

    var body: some View {
        switch family {
        case .systemSmall:
            SmallClusterWidget(entry: entry)
        case .systemMedium:
            MediumClusterWidget(entry: entry)
        case .systemLarge:
            LargeClusterWidget(entry: entry)
        case .accessoryInline:
            InlineClusterWidget(entry: entry)
        case .accessoryCircular:
            CircularClusterWidget(entry: entry)
        case .accessoryRectangular:
            RectangularClusterWidget(entry: entry)
        default:
            // A size the widget does not offer (`systemExtraLarge`, and anything
            // WidgetKit adds later) gets the medium layout. Every declared
            // family above has its own arm now: `default:` reaching a family
            // because it happens to be last is how `systemLarge` went unwritten.
            MediumClusterWidget(entry: entry)
        }
    }
}


extension ClusterMetricHeat {
    /// The one place a heat band becomes a colour.
    var color: Color {
        switch self {
        case .calm: return Theme.Semantic.ok
        case .warm: return Theme.Semantic.warn
        case .hot: return Theme.Semantic.bad
        case .unknown: return Theme.Semantic.onSurfaceMuted
        }
    }
}

/// One labelled metric token: "GPU 88%", coloured by its own heat band.
private struct MetricToken: View {
    let text: String
    let heat: ClusterMetricHeat
    var dimmed = false

    var body: some View {
        Text(text)
            .font(.hsccMono(10, weight: .semibold))
            .foregroundColor(dimmed ? Theme.Semantic.onSurfaceMuted : heat.color)
    }
}

// ---------------------------------------------------------------------------
// Small — state + nodes/models + work + topology glyph
// ---------------------------------------------------------------------------

struct SmallClusterWidget: View {
    let entry: ClusterEntry

    @ViewBuilder
    var body: some View {
        if !entry.configured || entry.paused {
            ClusterGatePane(entry: entry)
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
                .fill(entry.state.color)
                .frame(width: 8, height: 8)
            Text(entry.state.label)
                .font(.subheadline.weight(.semibold))
                .foregroundColor(Theme.Semantic.onSurface)
                .lineLimit(1)
                .minimumScaleFactor(0.8)
            Spacer(minLength: 0)
            if let age = entry.lastKnownAgeMinutes {
                Text("\(age)m")
                    .font(.caption2)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
        }
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
                        .font(.caption2.weight(.semibold))
                        .foregroundColor(Theme.Semantic.onSurface)
                }
                if entry.showSections.contains("models"), let models = entry.modelCount {
                    Text(ClusterMetricText.models(models))
                        .font(.caption2)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
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
                .font(.hsccMono(9, weight: .medium))
                .foregroundColor(Theme.Semantic.onSurface)
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
                        Image(systemName: "play.fill")
                            .font(.system(size: 7))
                            .foregroundColor(Theme.Semantic.ok)
                        Text("\(running)")
                            .font(.caption2.weight(.semibold))
                            .foregroundColor(Theme.Semantic.onSurface)
                    }
                }
                if let queue = entry.queueDepth {
                    HStack(spacing: 3) {
                        Image(systemName: "square.stack.3d.up")
                            .font(.system(size: 7))
                            .foregroundColor(Theme.Semantic.neutral)
                        Text("\(queue)")
                            .font(.caption2.weight(.semibold))
                            .foregroundColor(Theme.Semantic.onSurface)
                    }
                }
                Spacer(minLength: 0)
                if let blocked = entry.blockedCards, blocked > 0 {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .font(.system(size: 8))
                        .foregroundColor(Theme.Semantic.bad)
                }
            }
        }
    }

}

// ---------------------------------------------------------------------------
// Medium — topology pairs + models + nodes + metrics + idle remaining
// ---------------------------------------------------------------------------

struct MediumClusterWidget: View {
    let entry: ClusterEntry

    @ViewBuilder
    var body: some View {
        if !entry.configured || entry.paused {
            ClusterGatePane(entry: entry)
        } else {
            VStack(alignment: .leading, spacing: 8) {
                // State + the counts that matter at a glance.
                HStack(spacing: 5) {
                    Circle()
                        .fill(entry.state.color)
                        .frame(width: 8, height: 8)
                    Text(entry.state.label)
                        .font(.headline)
                        .foregroundColor(Theme.Semantic.onSurface)
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
                        .font(.caption2)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
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
                        Image(systemName: "play.circle.fill")
                            .font(.system(size: 10))
                            .foregroundColor(Theme.Semantic.ok)
                        metricLabel(value: "\(running)", label: "running")
                    }
                }
                if let queue = entry.queueDepth {
                    HStack(spacing: 4) {
                        Image(systemName: "square.stack.3d.up")
                            .font(.system(size: 10))
                            .foregroundColor(Theme.Semantic.neutral)
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
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.system(size: 10))
                .foregroundColor(Theme.Semantic.bad)
            Text("\(count)")
                .font(.caption.weight(.semibold))
                .foregroundColor(Theme.Semantic.bad)
        }
        .padding(.horizontal, 6)
        .padding(.vertical, 2)
        .background(Capsule().fill(Theme.Semantic.bad.opacity(0.14)))
    }

    private func miniPair(_ pair: TopologyPair, dimmed: Bool = false) -> some View {
        HStack(spacing: 4) {
            dot(pair.nodes[0], dimmed: dimmed)
            Rectangle()
                .fill(Theme.Palette.mist.opacity(0.5))
                .frame(width: 12, height: 2)
            dot(pair.nodes[1], dimmed: dimmed)
        }
        .overlay(alignment: .bottom) {
            Text(pair.role)
                .font(.system(size: 9))
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                .offset(y: 12)
        }
        .padding(.bottom, 6)
    }

    private func dot(_ node: TopologyNode, dimmed: Bool = false) -> some View {
        HStack(spacing: 3) {
            Circle()
                .fill(dimmed ? Theme.Palette.mist.opacity(0.55) : node.state.color)
                .frame(width: 8, height: 8)
            Text(node.label)
                .font(.hsccMono(10, weight: .semibold))
                .foregroundColor(dimmed ? Theme.Semantic.onSurfaceMuted : Theme.Semantic.onSurface)
        }
    }

    private func metricLabel(value: String, label: String) -> some View {
        VStack(spacing: 0) {
            Text(value)
                .font(.caption.weight(.semibold))
                .foregroundColor(Theme.Semantic.onSurface)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
            Text(label)
                .font(.system(size: 9))
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
        }
    }

}

// ---------------------------------------------------------------------------
// Large — every line the entry carries, each with a row of its own
// ---------------------------------------------------------------------------

/// The Home Screen large tile. It draws the SAME entry as the other five
/// families — no third read, no per-node table. WIDGET_DATA_CONTRACT §5.5's
/// `large` row asks for four nodes × three metrics plus the daemon-host line,
/// and this surface cannot serve that row: its projection aggregates the fleet
/// to the WORST host and keeps no per-host numbers (§7 redacts `hosts[].host`,
/// a LAN address), `/v1/cluster/status` is refused by name in
/// `scripts/cluster_widget_check.sh`, and that gate also pins the surface at two
/// reads — so a large-only `/v1/daemon/host` strip would tax every family's
/// refresh for one size. The extra rows therefore go to the fleet-level facts
/// the entry really carries, each drawn with its own label instead of folded
/// into one strip. Nil renders nothing or the em dash, never `0` (§6 rule 1).
struct LargeClusterWidget: View {
    let entry: ClusterEntry

    @ViewBuilder
    var body: some View {
        if !entry.configured || entry.paused {
            ClusterGatePane(entry: entry)
        } else {
            VStack(alignment: .leading, spacing: Theme.Spacing.sm.rawValue) {
                stateLine
                countsLine
                metricGrid
                topologyRows
                boardLine
                if let failure = entry.failure {
                    Text(failure.operatorLine)
                        .font(.footnote)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                        .lineLimit(2)
                        .accessibilityLabel(failure.operatorLine)
                }
                Spacer(minLength: 0)
            }
        }
    }

    /// The headline state, the autodown countdown and the age of anything
    /// last-known. The countdown is the reason this size exists: at 15 pt it is
    /// readable from across the room, at 9 pt in the medium strip it is not.
    private var stateLine: some View {
        HStack(spacing: Theme.Spacing.xs.rawValue) {
            Circle()
                .fill(entry.state.color)
                .frame(width: 10, height: 10)
            Text(entry.state.label)
                .font(.title3.weight(.semibold))
                .foregroundColor(Theme.Semantic.onSurface)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
            Spacer(minLength: 0)
            if let idle = entry.idleMinutesRemaining {
                Text("autodown in \(idle)m")
                    .font(.caption.weight(.medium))
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
            if let age = entry.lastKnownAgeMinutes {
                Text("last known \(age)m")
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
        }
        .accessibilityElement(children: .combine)
    }

    /// Nodes up over nodes listed, and models served. A count its read did not
    /// report drops out of the line rather than rendering a dash: with room for
    /// both, an absent number reads as an absent measurement, which is the truth.
    @ViewBuilder
    private var countsLine: some View {
        let parts = countsParts
        if !parts.isEmpty {
            Text(parts.joined(separator: "  ·  "))
                .font(.system(size: 15, weight: .semibold, design: .monospaced))
                .foregroundColor(Theme.Semantic.onSurface)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
                .accessibilityLabel(parts.joined(separator: ", "))
        }
    }

    private var countsParts: [String] {
        var parts: [String] = []
        if entry.showSections.contains("nodes"),
           let up = entry.nodesUp, let total = entry.nodesTotal {
            parts.append("\(up)/\(total) up")
        }
        if entry.showSections.contains("models"), let models = entry.modelCount {
            parts.append(ClusterMetricText.models(models))
        }
        return parts
    }

    /// The GPU / temperature / load metrics as tiles. `ClusterMetricText.tokens`
    /// already drops a metric no host reported, so an unmeasured number is an
    /// absent tile — never a dash next to the live ones and never a zero.
    @ViewBuilder
    private var metricGrid: some View {
        let tokens = ClusterMetricText.tokens(gpuUtilPct: entry.gpuUtilPct,
                                              gpuTempC: entry.gpuTempC,
                                              cpuTempC: entry.cpuTempC,
                                              load1m: entry.load1m)
        if entry.showSections.contains("nodes"), !tokens.isEmpty {
            LazyVGrid(columns: [GridItem(.flexible(), alignment: .leading),
                                GridItem(.flexible(), alignment: .leading)],
                      alignment: .leading,
                      spacing: Theme.Spacing.xs.rawValue) {
                ForEach(Array(tokens.enumerated()), id: \.offset) { _, token in
                    Text(token.label)
                        .font(.hsccMono(13, weight: .semibold))
                        .foregroundColor(entry.isStale ? Theme.Semantic.onSurfaceMuted : token.heat.color)
                        .lineLimit(1)
                        .minimumScaleFactor(0.7)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .accessibilityLabel(token.label)
                }
            }
        }
    }

    /// The two TP pairs with their roles named — the room the small tile's glyph
    /// does not have. Dimmed only when the strip came out of the last-known
    /// snapshot rather than this refresh (`pairsAreStale`), because these dots are
    /// otherwise a state glyph, not stored data.
    @ViewBuilder
    private var topologyRows: some View {
        if entry.showSections.contains("topology"), !entry.pairs.isEmpty {
            HStack(alignment: .top, spacing: Theme.Spacing.lg.rawValue) {
                ForEach(entry.pairs) { pair in
                    pairColumn(pair)
                }
                Spacer(minLength: 0)
            }
        }
    }

    private func pairColumn(_ pair: TopologyPair) -> some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs.rawValue) {
            HStack(spacing: Theme.Spacing.sm.rawValue) {
                ForEach(Array(pair.nodes.enumerated()), id: \.offset) { _, node in
                    HStack(spacing: 3) {
                        Circle()
                            .fill(entry.pairsAreStale ? Theme.Palette.mist.opacity(0.55) : node.state.color)
                            .frame(width: 9, height: 9)
                        Text(node.label)
                            .font(.hsccMono(11, weight: .semibold))
                            .foregroundColor(entry.pairsAreStale ? Theme.Semantic.onSurfaceMuted : Theme.Semantic.onSurface)
                    }
                }
            }
            Text(pair.role)
                .font(.caption2)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
        }
        .accessibilityElement(children: .combine)
    }

    /// The board row (dataset 3). Only the system families draw it — and only
    /// the system families pay the three kanban reads for it
    /// (`WidgetFamily.showsBoardWork`), so the size that has the room is the size
    /// that shows all three counts. A count whose read failed is absent.
    @ViewBuilder
    private var boardLine: some View {
        if entry.showSections.contains("board"),
           entry.runningCards != nil || entry.queueDepth != nil || (entry.blockedCards ?? 0) > 0 {
            HStack(spacing: Theme.Spacing.md.rawValue) {
                if let running = entry.runningCards {
                    boardToken(symbol: "play.circle.fill",
                               color: Theme.Semantic.ok,
                               value: running,
                               label: "running")
                }
                if let queue = entry.queueDepth {
                    boardToken(symbol: "square.stack.3d.up",
                               color: Theme.Semantic.neutral,
                               value: queue,
                               label: "queued")
                }
                if let blocked = entry.blockedCards, blocked > 0 {
                    boardToken(symbol: "exclamationmark.triangle.fill",
                               color: Theme.Semantic.bad,
                               value: blocked,
                               label: "blocked")
                }
                Spacer(minLength: 0)
            }
        }
    }

    private func boardToken(symbol: String, color: Color, value: Int, label: String) -> some View {
        HStack(spacing: 4) {
            Image(systemName: symbol)
                .font(.system(size: 12))
                .foregroundColor(color)
            Text("\(value)")
                .font(.system(size: 15, weight: .semibold, design: .monospaced))
                .foregroundColor(Theme.Semantic.onSurface)
            Text(label)
                .font(.caption2)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
        }
        .accessibilityLabel("\(value) \(label)")
    }
}

/// The one gate pane the three Home Screen sizes share: an unconfigured device is
/// invited to set the app up, a paused one says it is paused, and neither draws a
/// number. Small, medium and large each carried their own copy until t_b3ae3809
/// gave the surface a third size — three owners of one sentence is how three
/// sizes end up saying three different things.
private struct ClusterGatePane: View {
    let entry: ClusterEntry

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs.rawValue) {
            Image(systemName: entry.paused ? "pause.circle" : "gearshape")
                .font(.title3)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
            Text(entry.paused ? "Paused in Settings" : "Set up the app to see the cluster")
                .font(.caption2)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
        }
        .accessibilityElement(children: .combine)
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
    let entry: ClusterEntry

    var body: some View {
        if !entry.configured || entry.paused {
            Text(entry.shortGateLabel)
                .font(.system(size: 9, weight: .semibold))
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                .minimumScaleFactor(0.6)
                .accessibilityLabel(entry.shortGateLabel)
        } else if let up = entry.nodesUp, let total = entry.nodesTotal, total > 0 {
            Gauge(value: Double(min(max(up, 0), total)), in: 0...Double(total)) {
                Text("up")
            } currentValueLabel: {
                Text("\(up)/\(total)")
                    .font(.system(size: 11, weight: .semibold, design: .monospaced))
            }
            .gaugeStyle(.accessoryCircularCapacity)
            .tint(entry.state.color)
            .accessibilityLabel("\(up) of \(total) nodes up, \(entry.state.label)")
        } else if entry.state != .unknown {
            // No host list (the monitor degraded or the fleet is off) but the
            // state read answered: say the state, which IS the fact.
            Text(entry.state.label)
                .font(.system(size: 10, weight: .semibold))
                .foregroundColor(entry.state.color)
                .minimumScaleFactor(0.6)
                .multilineTextAlignment(.center)
                .accessibilityLabel(entry.state.label)
        } else {
            Text(entry.shortGateLabel)
                .font(.system(size: 9, weight: .semibold))
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                .minimumScaleFactor(0.6)
                .accessibilityLabel(entry.shortGateLabel)
        }
    }
}

/// The Lock Screen rectangular: state + nodes + models on line one, the GPU /
/// temperature / load strip on line two.
struct RectangularClusterWidget: View {
    let entry: ClusterEntry

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            if !entry.configured || entry.paused {
                Text(entry.shortGateLabel)
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    .lineLimit(1)
                Text("Open HSCC to configure the cluster")
                    .font(.system(size: 9))
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    .lineLimit(1)
            } else {
                HStack(spacing: 4) {
                    Circle()
                        .fill(entry.state.color)
                        .frame(width: 6, height: 6)
                    Text(lineOne)
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundColor(Theme.Semantic.onSurface)
                        .lineLimit(1)
                        .minimumScaleFactor(0.7)
                }
                .accessibilityLabel(lineOne)
                Text(lineTwo)
                    .font(.system(size: 10, weight: .medium, design: .monospaced))
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
        ClusterMetricHeat.percent(entry.gpuUtilPct).color
    }
}

/// The Lock Screen inline: one line, text only — nodes up, models served, GPU.
struct InlineClusterWidget: View {
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
    }
}

// ---------------------------------------------------------------------------
// Compact topology glyph
// ---------------------------------------------------------------------------

/// The compact topology glyph — the four nodes as two dotted TP pairs. Used by
/// the small widget and (with `dimmed`) the unreachable small widget so a
/// stale topology is clearly not-live.
struct MiniTopologyView: View {
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
                        .fill(Theme.Palette.mist.opacity(0.4))
                        .frame(width: 8, height: 2)
                    dot(pair.nodes[1])
                }
            }
            Spacer(minLength: 0)
        }
    }

    private func dot(_ node: TopologyNode) -> some View {
        Circle()
            .fill(dimmed ? Theme.Palette.mist.opacity(0.55) : node.state.color)
            .frame(width: 7, height: 7)
    }
}
