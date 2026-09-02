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
                    Spacer(minLength: 4)
                    MiniTopologyView(pairs: entry.pairs)
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
                }
            }
        }
    }

    private func miniPair(_ pair: TopologyPair) -> some View {
        HStack(spacing: 4) {
            dot(pair.nodes[0])
            Rectangle()
                .fill(Theme.Palette.mist.opacity(0.5))
                .frame(width: 12, height: 2)
            dot(pair.nodes[1])
        }
        .overlay(alignment: .bottom) {
            Text(pair.role)
                .font(.system(size: 9))
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                .offset(y: 12)
        }
        .padding(.bottom, 6)
    }

    private func dot(_ node: TopologyNode) -> some View {
        HStack(spacing: 3) {
            Circle()
                .fill(node.state.color)
                .frame(width: 8, height: 8)
            Text(node.label)
                .font(.hsccMono(10, weight: .semibold))
                .foregroundColor(Theme.Semantic.onSurface)
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
                        miniPair(pair)
                    }
                    Spacer(minLength: 0)
                }
            }
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
