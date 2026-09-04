import SwiftUI
import WidgetKit

// ---------------------------------------------------------------------------
// Cluster widget views — small + medium.
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
        .description("Cluster state at a glance — serving, waking, down, or can't reach.")
        .supportedFamilies([.systemSmall, .systemMedium])
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
        default:
            MediumClusterWidget(entry: entry)
        }
    }
}

// ---------------------------------------------------------------------------
// Small — state + compact topology glyph
// ---------------------------------------------------------------------------

struct SmallClusterWidget: View {
    let entry: ClusterEntry

    var body: some View {
        Group {
            if !entry.configured {
                unconfiguredView
            } else if entry.state == .unreachable {
                unreachableStateView
            } else {
                VStack(alignment: .leading, spacing: 8) {
                    stateLine
                    smallWorkLine
                    Spacer(minLength: 4)
                    MiniTopologyView(pairs: entry.pairs)
                }
            }
        }
    }

    /// Compact one-line "what the fleet is DOING" strip for the small widget:
    /// running + queue counts, with a red warning marker when there are blocked
    /// cards. Omitted entirely when no kanban data is present, so the small
    /// widget still fits its state + topology in a tight space.
    @ViewBuilder
    private var smallWorkLine: some View {
        let hasWork = entry.runningCards != nil || entry.queueDepth != nil || (entry.blockedCards ?? 0) > 0
        if hasWork {
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

    private var stateLine: some View {
        HStack(spacing: 5) {
            Circle()
                .fill(entry.state.color)
                .frame(width: 8, height: 8)
            Text(entry.state.label)
                .font(.subheadline.weight(.semibold))
                .foregroundColor(Theme.Semantic.onSurface)
            Spacer()
        }
    }

    private var unconfiguredView: some View {
        VStack(alignment: .leading, spacing: 6) {
            Image(systemName: "gearshape")
                .font(.title3)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
            Text("Set up the app to see the cluster")
                .font(.caption2)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
        }
    }

    private var unreachableStateView: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 5) {
                Circle()
                    .fill(entry.state.color)
                    .frame(width: 8, height: 8)
                Text("Can't reach the cluster")
                    .font(.caption.weight(.semibold))
                    .foregroundColor(Theme.Semantic.onSurface)
                Spacer()
            }
            if !entry.pairs.isEmpty {
                MiniTopologyView(pairs: entry.pairs, dimmed: true)
            }
            Spacer(minLength: 0)
            if let age = entry.lastKnownAgeMinutes {
                Text("last known \(age) min ago")
                    .font(.caption2)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Medium — topology pairs + model count + idle remaining
// ---------------------------------------------------------------------------

struct MediumClusterWidget: View {
    let entry: ClusterEntry

    var body: some View {
        Group {
            if !entry.configured {
                unconfiguredView
            } else if entry.state == .unreachable {
                unreachableStateView
            } else {
                VStack(alignment: .leading, spacing: 8) {
                    // State + the two metrics that matter.
                    HStack(spacing: 5) {
                        Circle()
                            .fill(entry.state.color)
                            .frame(width: 8, height: 8)
                        Text(entry.state.label)
                            .font(.headline)
                            .foregroundColor(Theme.Semantic.onSurface)
                        Spacer()
                        if let modelCount = entry.modelCount {
                            metricLabel(value: "\(modelCount)", label: "models")
                        }
                        if let idle = entry.idleMinutesRemaining {
                            metricLabel(value: "\(idle)m", label: "to autodown")
                        }
                    }
                    // The topology pairs with per-node colour.
                    HStack(spacing: 16) {
                        ForEach(entry.pairs) { pair in
                            miniPair(pair)
                        }
                        Spacer(minLength: 0)
                    }
                    // What the fleet is DOING: running cards, queue depth, and a
                    // failure indicator (blocked cards needing attention).
                    workMetricsRow
                }
            }
        }
    }

    /// The board work row: running cards + queue depth as neutral metrics, and
    /// a red failure badge when blocked cards need attention. Each count is
    /// optional — a failed kanban fetch simply omits that metric.
    @ViewBuilder
    private var workMetricsRow: some View {
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
            Text(label)
                .font(.system(size: 9))
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
        }
    }

    private var unconfiguredView: some View {
        VStack(alignment: .leading, spacing: 6) {
            Image(systemName: "gearshape")
                .font(.title3)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
            Text("Set up the app to see the cluster")
                .font(.caption2)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
        }
    }

    private var unreachableStateView: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 5) {
                Circle()
                    .fill(entry.state.color)
                    .frame(width: 8, height: 8)
                Text("Can't reach the cluster")
                    .font(.subheadline.weight(.semibold))
                    .foregroundColor(Theme.Semantic.onSurface)
                Spacer()
                if let age = entry.lastKnownAgeMinutes {
                    Text("\(age) min ago")
                        .font(.caption2)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
            }
            if !entry.pairs.isEmpty {
                HStack(spacing: 16) {
                    ForEach(entry.pairs) { pair in
                        miniPair(pair, dimmed: true)
                    }
                    Spacer(minLength: 0)
                }
            }
            // Last-known board work, clearly dimmed-for-stale like the topology:
            // the operator still sees what the fleet WAS doing while it's down.
            workMetricsRow
        }
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
    /// the cluster is currently unreachable).
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
