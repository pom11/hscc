import SwiftUI
import WidgetKit

// ---------------------------------------------------------------------------
// Cluster widget views.
//
// Six families, one entry, and one text model (`ClusterEntry.text(for:)`).
// The layout only decides how much of that text fits:
//
//   small      — one headline tally + the models/throughput line
//   medium     — the same line plus up to four node rows
//   large      — every node row with CPU/GPU temps and run/wait
//   inline     — the headline + detail on one Lock Screen line
//   circular   — the up/total gauge
//   rectangular— headline, detail, the first two node rows
//
// No view composes a number. `scripts/cluster_widget_check.sh` executes the
// text model against the committed fixture and asserts each family's lines.
// ---------------------------------------------------------------------------

struct ClusterWidget: Widget {
    let kind = "ClusterWidget"

    var body: some WidgetConfiguration {
        let style = WidgetStyle.resolved(for: .widgetCluster)
        return StaticConfiguration(kind: kind, provider: ClusterTimelineProvider()) { entry in
            ClusterWidgetView(entry: entry)
                .containerBackground(for: .widget) {
                    WidgetSurfaceBackground(style: style)
                }
                .widgetRenderingAdaptation(style: style)
        }
        .configurationDisplayName("Cluster")
        .description("Nodes up, the model each node serves, throughput and temperatures.")
        #if os(iOS)
        .supportedFamilies([.systemSmall, .systemMedium, .systemLarge,
                            .accessoryCircular, .accessoryRectangular,
                            .accessoryInline])
        #else
        .supportedFamilies([.systemSmall, .systemMedium, .systemLarge,
                            .systemExtraLarge])
        #endif
    }
}

struct ClusterWidgetView: View {
    @Environment(\.widgetStyle) private var style
    let entry: ClusterEntry
    @Environment(\.widgetFamily) private var family

    var body: some View {
        switch family {
        case .systemSmall:
            SmallClusterWidget(entry: entry)
        #if os(iOS)
        case .accessoryInline:
            InlineClusterWidget(entry: entry)
        case .accessoryCircular:
            CircularClusterWidget(entry: entry)
        case .accessoryRectangular:
            RectangularClusterWidget(entry: entry)
        #endif
        case .systemMedium:
            MediumClusterWidget(entry: entry)
        case .systemLarge:
            LargeClusterWidget(entry: entry)
        case .systemExtraLarge:
            LargeClusterWidget(entry: entry)
        default:
            MediumClusterWidget(entry: entry)
        }
    }
}

extension ClusterMetricHeat {
    func color(_ style: WidgetStyle) -> Color {
        switch self {
        case .calm: return style.okColor
        case .warm: return style.warnColor
        case .hot: return style.badColor
        case .unknown: return style.secondaryTextColor
        }
    }
}

private struct ClusterHeadline: View {
    @Environment(\.widgetStyle) private var style
    let entry: ClusterEntry
    let size: CGFloat

    var body: some View {
        Text(entry.text(for: .small).headline)
            .font(style.font(size: size, weight: .bold))
            .foregroundColor(entry.heat.color(style))
            .lineLimit(1)
            .minimumScaleFactor(0.55)
            .accessibilityLabel(entry.text(for: .small).headline)
    }
}

private struct ClusterDetail: View {
    @Environment(\.widgetStyle) private var style
    let entry: ClusterEntry
    let surface: FleetSurface

    var body: some View {
        if let detail = entry.text(for: surface).detail {
            Text(detail)
                .font(style.font(.caption, weight: .medium))
                .foregroundColor(style.primaryTextColor)
                .lineLimit(1)
                .minimumScaleFactor(0.65)
        }
    }
}

private struct ClusterFooter: View {
    @Environment(\.widgetStyle) private var style
    let entry: ClusterEntry
    let surface: FleetSurface

    var body: some View {
        if let footer = entry.text(for: surface).footer {
            Text(footer)
                .font(style.font(.caption2))
                .foregroundColor(style.secondaryTextColor)
                .lineLimit(2)
        }
    }
}

/// One node: state dot, label/model title, and the metrics line.
private struct ClusterNodeRowView: View {
    @Environment(\.widgetStyle) private var style
    let row: FleetRow
    let node: FleetNodeLine?
    var compact = false

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Circle()
                .fill((node.map { ClusterMetricHeat.node($0) } ?? .unknown).color(style))
                .frame(width: 7, height: 7)
            VStack(alignment: .leading, spacing: 1) {
                Text(row.title)
                    .font(style.font(size: compact ? 10 : 11, weight: .semibold))
                    .foregroundColor(style.primaryTextColor)
                    .lineLimit(1)
                    .minimumScaleFactor(0.7)
                if let detail = row.detail {
                    Text(detail)
                        .font(style.font(size: compact ? 9 : 10))
                        .foregroundColor(style.secondaryTextColor)
                        .lineLimit(1)
                        .minimumScaleFactor(0.7)
                }
            }
            Spacer(minLength: 0)
        }
    }
}

// ---------------------------------------------------------------------------
// Small — one headline number, one supporting line.
// ---------------------------------------------------------------------------

struct SmallClusterWidget: View {
    @Environment(\.widgetStyle) private var style
    let entry: ClusterEntry

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 4) {
                Image(systemName: "square.stack.3d.up").widgetIcon(style)
                    .font(style.font(size: 9))
                    .foregroundColor(style.secondaryTextColor)
                Text("CLUSTER")
                    .font(style.font(size: 9, weight: .semibold))
                    .foregroundColor(style.secondaryTextColor)
            }
            Spacer(minLength: 0)
            ClusterHeadline(entry: entry, size: 32)
            ClusterDetail(entry: entry, surface: .small)
            Spacer(minLength: 0)
            ClusterFooter(entry: entry, surface: .small)
        }
        .widgetHeader(style)
    }
}

// ---------------------------------------------------------------------------
// Medium — headline + up to four node rows.
// ---------------------------------------------------------------------------

struct MediumClusterWidget: View {
    @Environment(\.widgetStyle) private var style
    let entry: ClusterEntry

    var body: some View {
        let text = entry.text(for: .medium)
        VStack(alignment: .leading, spacing: 5) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Image(systemName: "square.stack.3d.up").widgetIcon(style)
                ClusterHeadline(entry: entry, size: 26)
                Spacer(minLength: 0)
                ClusterDetail(entry: entry, surface: .medium)
            }
            .widgetHeader(style)
            if !text.rows.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(Array(text.rows.enumerated()), id: \.offset) { index, row in
                        ClusterNodeRowView(row: row, node: node(at: index), compact: true)
                    }
                }
            }
            Spacer(minLength: 0)
            ClusterFooter(entry: entry, surface: .medium)
        }
    }

    private func node(at index: Int) -> FleetNodeLine? {
        guard let nodes = entry.fleet?.nodes, nodes.indices.contains(index) else { return nil }
        return nodes[index]
    }
}

// ---------------------------------------------------------------------------
// Large / macOS XL — every node row, full metrics, failure/age line.
// ---------------------------------------------------------------------------

struct LargeClusterWidget: View {
    @Environment(\.widgetStyle) private var style
    let entry: ClusterEntry

    var body: some View {
        let text = entry.text(for: .large)
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                Image(systemName: "square.stack.3d.up").widgetIcon(style)
                ClusterHeadline(entry: entry, size: 30)
                Spacer(minLength: 0)
                ClusterDetail(entry: entry, surface: .large)
            }
            .widgetHeader(style)
            if !text.rows.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(Array(text.rows.enumerated()), id: \.offset) { index, row in
                        ClusterNodeRowView(row: row, node: node(at: index))
                        if index < text.rows.count - 1 {
                            Divider().opacity(0.25)
                        }
                    }
                }
            } else if entry.failure == nil {
                Text("No node sample in this refresh.")
                    .font(style.font(.caption))
                    .foregroundColor(style.secondaryTextColor)
            }
            Spacer(minLength: 0)
            ClusterFooter(entry: entry, surface: .large)
        }
    }

    private func node(at index: Int) -> FleetNodeLine? {
        guard let nodes = entry.fleet?.nodes, nodes.indices.contains(index) else { return nil }
        return nodes[index]
    }
}

// ---------------------------------------------------------------------------
// Lock Screen accessories
// ---------------------------------------------------------------------------

/// One clear headline number: the up/total gauge.
struct CircularClusterWidget: View {
    @Environment(\.widgetStyle) private var style
    let entry: ClusterEntry

    var body: some View {
        if let fleet = entry.fleet,
           let up = fleet.tally.nodesUp, let total = fleet.tally.nodesTotal, total > 0 {
            Gauge(value: Double(min(max(up, 0), total)), in: 0...Double(total)) {
                Text("up")
            } currentValueLabel: {
                Text("\(up)/\(total)")
                    .font(style.font(size: 11, weight: .semibold))
            }
            .gaugeStyle(.accessoryCircularCapacity)
            .tint(.primary)
            .accessibilityLabel("\(up) of \(total) nodes up")
        } else {
            Text(entry.text(for: .circular).headline)
                .font(style.font(size: 9, weight: .semibold))
                .foregroundColor(.secondary)
                .minimumScaleFactor(0.6)
                .multilineTextAlignment(.center)
                .accessibilityLabel(entry.text(for: .circular).headline)
        }
    }
}

/// Two lines: the tally + models, then throughput or the failure reason.
struct RectangularClusterWidget: View {
    @Environment(\.widgetStyle) private var style
    let entry: ClusterEntry

    var body: some View {
        let text = entry.text(for: .rectangular)
        VStack(alignment: .leading, spacing: 2) {
            Text(text.headline)
                .font(style.font(size: 12, weight: .semibold))
                .foregroundColor(.primary)
                .lineLimit(1)
            if let detail = text.detail {
                Text(detail)
                    .font(style.font(size: 10, weight: .medium))
                    .foregroundColor(.primary)
                    .lineLimit(1)
                    .minimumScaleFactor(0.7)
            }
            ForEach(Array(text.rows.prefix(1).enumerated()), id: \.offset) { _, row in
                Text(row.title + (row.detail.map { " · \($0)" } ?? ""))
                    .font(style.font(size: 9))
                    .foregroundColor(.secondary)
                    .lineLimit(1)
                    .minimumScaleFactor(0.7)
            }
            if let footer = text.footer {
                Text(footer)
                    .font(style.font(size: 9))
                    .foregroundColor(.secondary)
                    .lineLimit(1)
            }
            Spacer(minLength: 0)
        }
    }
}

/// One line: headline and detail, nothing truncated into soup.
struct InlineClusterWidget: View {
    @Environment(\.widgetStyle) private var style
    let entry: ClusterEntry

    var body: some View {
        Text(entry.inlineLine)
            .font(style.font(.caption))
            .accessibilityLabel(entry.inlineLine)
    }
}
