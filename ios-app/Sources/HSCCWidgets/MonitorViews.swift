import SwiftUI
import WidgetKit

// ---------------------------------------------------------------------------
// HSCC Monitor widget views — small, medium, large, XL (macOS) and the three
// Lock Screen accessories.
//
// One text model (`MonitorEntry.text(for:)`) drives every family, so the
// circular gauge, the Lock Screen line and the large table cannot disagree.
// The headline is the node tally; the detail is the models/throughput line;
// the rows are the CLI's per-node host/serving rows, except on `systemMedium`,
// which draws `hscc cluster monitor`'s host table itself
// (`CLIMonitorHostTableView`); the footer is the daemon host's health block.
// The three Lock Screen families draw that command's four glance numbers
// (hottest temperature, fleet GPU load, fleet watts, running/waiting) through
// `CLILockScreenCopy`, and never the daemon-host half.
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
        .supportedFamilies([.systemSmall, .systemMedium, .systemLarge,
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
            LargeMonitorWidget(entry: entry)
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
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Image(systemName: "gauge.with.dots.needle.bottom.50percent").widgetIcon(style)
                    .font(style.font(size: 9))
                    .foregroundColor(style.secondaryTextColor)
                // Same trade as the cluster tile's medium: the numeral gives
                // nine columns of CLI text the room it was spending on itself.
                MonitorHeadline(entry: entry, size: 15)
                Spacer(minLength: 0)
                MonitorDetail(entry: entry, surface: .medium)
            }
            .widgetHeader(style)
            // The CLI's host table, the same view the cluster tile's medium
            // draws (`CLIMonitorHostTableView`) — one table for both tiles, so
            // the two surfaces cannot grow different ideas of what the CLI
            // prints. The daemon-host line stays in the footer, where
            // `MonitorEntry.text(for:)` puts it.
            if let rows = entry.cliHostRows {
                CLIMonitorHostTableView(rows: rows)
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
            if !text.rows.isEmpty {
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

/// The Lock Screen ring: the fleet's GPU load as a capacity gauge (a percentage
/// is a ring's own shape), with the wattage it costs underneath as the
/// secondary — the same two numbers the Cluster tile's ring draws, from the same
/// projection, because it is the same route. The daemon host has no pixels here:
/// an accessory refresh refuses that request, and a ring has no room for a second
/// machine's health. A ring with no measured percentage draws the gated line,
/// never an empty ring (which would read as an idle fleet).
struct CircularMonitorWidget: View {
    @Environment(\.widgetStyle) private var style
    let entry: MonitorEntry

    var body: some View {
        let text = entry.text(for: .circular)
        if let fill = entry.lockScreenGaugeValue {
            VStack(spacing: 0) {
                Gauge(value: min(max(fill, 0), 100), in: 0...100) {
                    Text(CLILockScreenCopy.gaugeLabel)
                } currentValueLabel: {
                    Text(text.headline)
                        .font(style.font(size: 11, weight: .semibold))
                }
                .gaugeStyle(.accessoryCircularCapacity)
                .tint(.primary)
                if let watts = text.detail {
                    Text(watts)
                        .font(style.font(size: 8))
                        .foregroundStyle(.secondary)
                }
            }
            .accessibilityLabel(entry.lockScreenVoice ?? text.headline)
        } else {
            Text(text.headline)
                .font(style.font(size: 9, weight: .semibold))
                .foregroundColor(.secondary)
                .minimumScaleFactor(0.6)
                .multilineTextAlignment(.center)
                .accessibilityLabel(entry.lockScreenVoice ?? text.headline)
        }
    }
}

/// The Lock Screen row: the CLI's metrics (hottest temperature, fleet GPU load,
/// fleet watts, each with its `n/N` coverage when only some hosts reported),
/// then the queue and the host count it covers, then the age or the failure.
/// The daemon-host half never reaches this line — see the provider's family
/// rule — and every string is `entry.text(for:)`, so the two tiles cannot
/// disagree about the same fleet.
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
                .minimumScaleFactor(0.7)
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
        .accessibilityLabel(entry.lockScreenVoice ?? text.headline)
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
