import SwiftUI
import WidgetKit

// ---------------------------------------------------------------------------
// HSCC Monitor widget views — small, medium, large, XL (macOS) and the three
// Lock Screen accessories.
//
// One text model (`MonitorEntry.text(for:)`) drives every family, so the
// circular gauge, the Lock Screen line and the large table cannot disagree.
// The headline is the node tally; the detail is the models/throughput line;
// the rows are the CLI's per-node host/serving rows; the footer is the daemon
// host's health block.
// ---------------------------------------------------------------------------

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
        .description("Per-node model, load and temperature, plus the daemon host.")
        #if os(iOS)
        // .systemExtraLarge is the iPad size (t_9e64d9d9), where the two CLI
        // tables fit side by side.
        .supportedFamilies([.systemSmall, .systemMedium, .systemLarge, .systemExtraLarge,
                            .accessoryInline, .accessoryCircular, .accessoryRectangular])
        #else
        .supportedFamilies([.systemSmall, .systemMedium, .systemLarge,
                            .systemExtraLarge])
        #endif
    }
}

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
        case .systemMedium:
            MediumMonitorWidget(entry: entry)
        case .systemLarge:
            LargeMonitorWidget(entry: entry)
        case .systemExtraLarge:
            ExtraLargeMonitorWidget(entry: entry)
        default:
            MediumMonitorWidget(entry: entry)
        }
    }
}

extension WidgetFamily {
    var showsDaemonHost: Bool {
        switch self {
        case .systemSmall, .systemMedium, .systemLarge, .systemExtraLarge:
            return true
        default:
            return false
        }
    }
}

private struct MonitorHeadline: View {
    @Environment(\.widgetStyle) private var style
    let entry: MonitorEntry
    let size: CGFloat

    var body: some View {
        Text(entry.text(for: .small).headline)
            .font(style.font(size: size, weight: .bold))
            .foregroundColor(entry.state.color(style))
            .lineLimit(1)
            .minimumScaleFactor(0.55)
            .accessibilityLabel(entry.text(for: .small).headline)
    }
}

private struct MonitorDetail: View {
    @Environment(\.widgetStyle) private var style
    let entry: MonitorEntry
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

private struct MonitorFooter: View {
    @Environment(\.widgetStyle) private var style
    let entry: MonitorEntry
    let surface: FleetSurface

    var body: some View {
        if let footer = entry.text(for: surface).footer {
            Text(footer)
                .font(style.font(.caption2))
                .foregroundColor(style.secondaryTextColor)
                .lineLimit(1)
                .minimumScaleFactor(0.65)
        }
    }
}

private struct MonitorNodeRowView: View {
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

private func node(of entry: MonitorEntry, at index: Int) -> FleetNodeLine? {
    guard let nodes = entry.fleet?.nodes, nodes.indices.contains(index) else { return nil }
    return nodes[index]
}

struct SmallMonitorWidget: View {
    @Environment(\.widgetStyle) private var style
    let entry: MonitorEntry

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 4) {
                Image(systemName: "gauge.with.dots.needle.bottom.50percent").widgetIcon(style)
                    .font(style.font(size: 9))
                    .foregroundColor(style.secondaryTextColor)
                Text("MONITOR")
                    .font(style.font(size: 9, weight: .semibold))
                    .foregroundColor(style.secondaryTextColor)
            }
            Spacer(minLength: 0)
            MonitorHeadline(entry: entry, size: 32)
            MonitorDetail(entry: entry, surface: .small)
            Spacer(minLength: 0)
            MonitorFooter(entry: entry, surface: .small)
        }
        .widgetHeader(style)
    }
}

struct MediumMonitorWidget: View {
    @Environment(\.widgetStyle) private var style
    let entry: MonitorEntry

    var body: some View {
        let text = entry.text(for: .medium)
        VStack(alignment: .leading, spacing: 5) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Image(systemName: "gauge.with.dots.needle.bottom.50percent").widgetIcon(style)
                MonitorHeadline(entry: entry, size: 26)
                Spacer(minLength: 0)
                MonitorDetail(entry: entry, surface: .medium)
            }
            .widgetHeader(style)
            if !text.rows.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(Array(text.rows.enumerated()), id: \.offset) { index, row in
                        MonitorNodeRowView(row: row, node: node(of: entry, at: index), compact: true)
                    }
                }
            }
            Spacer(minLength: 0)
            MonitorFooter(entry: entry, surface: .medium)
        }
    }
}

struct LargeMonitorWidget: View {
    @Environment(\.widgetStyle) private var style
    let entry: MonitorEntry

    var body: some View {
        let text = entry.text(for: .large)
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                Image(systemName: "gauge.with.dots.needle.bottom.50percent").widgetIcon(style)
                MonitorHeadline(entry: entry, size: 30)
                Spacer(minLength: 0)
                MonitorDetail(entry: entry, surface: .large)
            }
            .widgetHeader(style)
            if let tables = entry.tables {
                // The CLI's own tables replace the summary rows: same hosts,
                // every printed column, and the serving rows the rows used to
                // paraphrase.
                MonitorTablesBlock(tables: tables, surface: .large)
            } else if !text.rows.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(Array(text.rows.enumerated()), id: \.offset) { index, row in
                        MonitorNodeRowView(row: row, node: node(of: entry, at: index))
                        if index < text.rows.count - 1 {
                            Divider().opacity(0.25)
                        }
                    }
                }
            }
            Spacer(minLength: 0)
            MonitorFooter(entry: entry, surface: .large)
        }
    }
}

/// iPad / macOS extra-large: the two tables side by side, and the six sample
/// fields the CLI's host table prints no column for. The footer keeps the
/// daemon sentence `text(for:)` appends — this size does not drop it.
struct ExtraLargeMonitorWidget: View {
    @Environment(\.widgetStyle) private var style
    let entry: MonitorEntry

    var body: some View {
        let text = entry.text(for: .extraLarge)
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline, spacing: 12) {
                Image(systemName: "gauge.with.dots.needle.bottom.50percent").widgetIcon(style)
                MonitorHeadline(entry: entry, size: 34)
                Spacer(minLength: 0)
                MonitorDetail(entry: entry, surface: .extraLarge)
            }
            .widgetHeader(style)
            if let tables = entry.tables {
                MonitorTablesBlock(tables: tables, surface: .extraLarge)
            } else if !text.rows.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(Array(text.rows.enumerated()), id: \.offset) { index, row in
                        MonitorNodeRowView(row: row, node: node(of: entry, at: index))
                    }
                }
            }
            Spacer(minLength: 0)
            MonitorFooter(entry: entry, surface: .extraLarge)
        }
    }
}

/// One clear headline number: nodes up over nodes listed.
struct CircularMonitorWidget: View {
    @Environment(\.widgetStyle) private var style
    let entry: MonitorEntry

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

struct RectangularMonitorWidget: View {
    @Environment(\.widgetStyle) private var style
    let entry: MonitorEntry

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

struct InlineMonitorWidget: View {
    @Environment(\.widgetStyle) private var style
    let entry: MonitorEntry

    var body: some View {
        Text(entry.inlineLine)
            .font(style.font(.caption))
            .accessibilityLabel(entry.inlineLine)
    }
}
