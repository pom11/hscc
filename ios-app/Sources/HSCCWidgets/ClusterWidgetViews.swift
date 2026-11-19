import SwiftUI
import WidgetKit

// ---------------------------------------------------------------------------
// Cluster widget views.
//
// Six families, one entry, and one text model (`ClusterEntry.text(for:)`),
// plus the CLI's own host table on the one size that shows it
// (`CLIMonitorHostTableView`). The layout decides how much of that text fits:
//
//   small      — the DENSE GRID (t_9e110d88): up to four hosts x the CLI's GPU%,
//                GPU Temp, CPU Temp and GPU Power columns, plus the serving
//                run/wait totals. `ClusterSmallGridView` draws it; when there
//                are no rows to grid the family shows the gate pane instead.
//   medium     — the CLI's host table: nine columns, one row per host
//   large      — every node row with CPU/GPU temps and run/wait
//   inline     — the headline + detail on one Lock Screen line
//   circular   — the up/total gauge
//   rectangular— headline, detail, the first two node rows
//
// No view composes a number. `scripts/cluster_widget_check.sh` executes the
// text model against the committed fixture and asserts each family's lines, and
// `scripts/model_decode_check.sh` executes the small grid's own cell model
// (`ClusterSmallGrid.swift`) against the CLI goldens.
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
        // .systemExtraLarge is the iPad size (t_9e64d9d9): the iPhone cannot
        // host it, but the same widget on iPad gets the two-column tables.
        .supportedFamilies([.systemSmall, .systemMedium, .systemLarge, .systemExtraLarge,
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
            ExtraLargeClusterWidget(entry: entry)
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
// Small — the dense grid: up to four hosts x four CLI columns + run/wait.
//
// This is the family the "one number" failure belongs to, so it draws the
// monitor payload rather than a summary of it: `ClusterSmallGridView` renders
// the CLI's own GPU%, GPU Temp, CPU Temp and GPU Power cells for up to four
// hosts, with the serving totals underneath and the hosts it could not fit
// counted rather than dropped.
//
// The gate pane is the family's OTHER branch, and it is reached the same way for
// all three no-data states: `ClusterEntry.smallTile` is nil when the entry is
// unconfigured, paused or has no rows, and `ClusterEntry.text(for:)` — which
// checks `!configured` before any paused copy — owns what that pane says. So the
// invite-first invariant has one owner here too, and no branch can grid dashes
// over a paused surface.
// ---------------------------------------------------------------------------

struct SmallClusterWidget: View {
    @Environment(\.widgetStyle) private var style
    let entry: ClusterEntry

    var body: some View {
        if let tile = entry.smallTile {
            // The tally and the age line come from the entry's own text model —
            // this family does not compose a sentence, it lays one out.
            let text = entry.text(for: .small)
            ClusterSmallGridView(tile: tile,
                                 headerIcon: "square.stack.3d.up",
                                 headerWord: "CLUSTER",
                                 tally: text.headline,
                                 tallyColor: entry.heat.color(style),
                                 footer: text.footer)
        } else {
            gatePane
        }
    }

    /// Nothing to grid. The entry's gated text model says why: the setup
    /// invite, "Paused", or the reason this refresh is not live.
    private var gatePane: some View {
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
            ClusterHeadline(entry: entry, size: 20)
            ClusterDetail(entry: entry, surface: .small)
            Spacer(minLength: 0)
            ClusterFooter(entry: entry, surface: .small)
        }
        .widgetHeader(style)
    }
}

// ---------------------------------------------------------------------------
// Medium — the CLI's own host table: `host_table`'s nine columns, one row per
// host, under the CLI's own header. This is the "looks like the CLI" size
// (t_2d1c12dc): no abbreviations, no re-rounding, no invented units — the
// strings are `CLIMonitorHostRow.cells` and the titles are
// `CLIMonitorHostRow.columns`, both from the shared view-model.
// ---------------------------------------------------------------------------

struct MediumClusterWidget: View {
    @Environment(\.widgetStyle) private var style
    let entry: ClusterEntry

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Image(systemName: "square.stack.3d.up").widgetIcon(style)
                    .font(style.font(size: 9))
                    .foregroundColor(style.secondaryTextColor)
                // A table needs the vertical room the 26 pt numeral used to
                // take; the tally is still the loudest thing on the tile, it is
                // just no longer taller than the rows under it.
                ClusterHeadline(entry: entry, size: 15)
                Spacer(minLength: 0)
                ClusterDetail(entry: entry, surface: .medium)
            }
            .widgetHeader(style)
            // `cliHostRows` is nil for every state that must NOT draw numbers —
            // unconfigured, paused, the "nodes" section off, no host list — and
            // the headline plus footer carry those instead. There is no
            // fallback row list here on purpose: the glance rows and this table
            // are two projections of one response, so drawing them when the
            // table is switched off would make the toggle a lie.
            if let rows = entry.cliHostRows {
                CLIMonitorHostTableView(rows: rows)
            }
            Spacer(minLength: 0)
            ClusterFooter(entry: entry, surface: .medium)
        }
    }
}

// ---------------------------------------------------------------------------
// Large — the CLI's host table + serving table, stacked.
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
            if let tables = entry.tables {
                // The tables replace the node rows: they carry the same hosts
                // plus every column `cluster monitor` prints, and the text rows
                // were a summary of exactly this data.
                MonitorTablesBlock(tables: tables, surface: .large)
            } else if !text.rows.isEmpty {
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
// Extra large (iPad + macOS) — the two tables side by side, plus the six
// fields the CLI's host table has no column for.
// ---------------------------------------------------------------------------

struct ExtraLargeClusterWidget: View {
    @Environment(\.widgetStyle) private var style
    let entry: ClusterEntry

    var body: some View {
        let text = entry.text(for: .extraLarge)
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline, spacing: 12) {
                Image(systemName: "square.stack.3d.up").widgetIcon(style)
                ClusterHeadline(entry: entry, size: 34)
                Spacer(minLength: 0)
                ClusterDetail(entry: entry, surface: .extraLarge)
            }
            .widgetHeader(style)
            if let tables = entry.tables {
                MonitorTablesBlock(tables: tables, surface: .extraLarge)
            } else if !text.rows.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(Array(text.rows.enumerated()), id: \.offset) { _, row in
                        ClusterNodeRowView(row: row, node: nil)
                    }
                }
            } else if entry.failure == nil {
                Text("No node sample in this refresh.")
                    .font(style.font(.caption))
                    .foregroundColor(style.secondaryTextColor)
            }
            Spacer(minLength: 0)
            ClusterFooter(entry: entry, surface: .extraLarge)
        }
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
