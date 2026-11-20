import SwiftUI
import WidgetKit

// ---------------------------------------------------------------------------
// Cluster widget views — C1 Health line (2026-11-20 operator pick).
//
// small  — "Cluster", the fleet in one word, tok/s and the hottest temperature.
// medium — a 112 pt tally column and one row per node: name, util meter, temp.
// large  — header + now tok/s, the 2 h total-throughput area chart, the node
//          table (node ◆ orchestrator, util, temp, power, tok/s), and a footer
//          naming the models and daemon state.
//
// The Lock Screen accessory bodies below keep their previous layout; the
// 2026-11-20 redesign of those families is a later card. They still draw the
// existing gated text model and the same monitor read as the Home Screen tile.
// ---------------------------------------------------------------------------

struct ClusterWidget: Widget {
    let kind = "ClusterWidget"

    var body: some WidgetConfiguration {
        StaticConfiguration(kind: kind, provider: ClusterTimelineProvider()) { entry in
            ClusterWidgetView(entry: entry)
                .containerBackground(.fill.tertiary, for: .widget)
        }
        .configurationDisplayName("Cluster")
        .description("Nodes up, throughput, the hottest node and the 2 h trend.")
        #if os(iOS)
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

private enum ClusterC1Ink {
    static func headline(_ c1: ClusterC1) -> Color {
        guard let up = c1.up, let total = c1.total, total > 0 else { return .secondary }
        if up == 0 { return NativeStyle.bad }
        return up == total ? NativeStyle.ok : NativeStyle.warn
    }

    static func temperature(_ celsius: Double?) -> Color {
        NativeStyle.temperature(celsius)
    }
}

private struct ClusterC1Label: View {
    let text: String

    var body: some View {
        Text(text)
            .font(NativeStyle.label())
            .foregroundStyle(.secondary)
            .lineLimit(1)
    }
}

/// The one gate pane every Home Screen family shows when the surface cannot
/// draw data: the setup invite, "Paused", or why this refresh is not live.
private struct ClusterC1Gate: View {
    let entry: ClusterEntry

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ClusterC1Label(text: "Cluster")
            Spacer(minLength: 0)
            Text(entry.shortGateLabel)
                .font(NativeStyle.hero(24))
                .foregroundStyle(.secondary)
                .lineLimit(2)
                .minimumScaleFactor(0.7)
            if let reason = entry.failure?.shortLabel {
                Text(reason)
                    .font(NativeStyle.small())
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }
            Spacer(minLength: 0)
            AsOfStamp(date: nil)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
    }
}

// MARK: - Small

struct SmallClusterWidget: View {
    let entry: ClusterEntry

    var body: some View {
        if !entry.configured || entry.paused {
            ClusterC1Gate(entry: entry)
        } else {
            smallBody
        }
    }

    private var smallBody: some View {
        let c1 = entry.c1
        return VStack(alignment: .leading, spacing: 4) {
            ClusterC1Label(text: "Cluster")
            Spacer(minLength: 2)
            Text(c1.headline)
                .font(NativeStyle.hero())
                .foregroundStyle(ClusterC1Ink.headline(c1))
                .lineLimit(1)
                .minimumScaleFactor(0.55)
            Text(c1.tokText)
                .font(NativeStyle.sub())
                .monospacedDigit()
                .lineLimit(1)
            Text(c1.hottestText)
                .font(NativeStyle.sub())
                .monospacedDigit()
                .foregroundStyle(ClusterC1Ink.temperature(c1.hottestC))
                .lineLimit(1)
            Spacer(minLength: 2)
            AsOfStamp(date: c1.dataAt)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
        .stale(c1.stale)
    }
}

// MARK: - Medium

struct MediumClusterWidget: View {
    let entry: ClusterEntry

    var body: some View {
        if !entry.configured || entry.paused {
            ClusterC1Gate(entry: entry)
        } else {
            mediumBody
        }
    }

    private var mediumBody: some View {
        let c1 = entry.c1
        return HStack(alignment: .top, spacing: 14) {
            VStack(alignment: .leading, spacing: 4) {
                ClusterC1Label(text: "Cluster")
                Text(c1.tally)
                    .font(NativeStyle.hero(30))
                    .foregroundStyle(ClusterC1Ink.headline(c1))
                    .monospacedDigit()
                    .lineLimit(1)
                Text("serving")
                    .font(NativeStyle.small())
                    .foregroundStyle(.secondary)
                Text(c1.tokText)
                    .font(NativeStyle.small())
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
                Spacer(minLength: 2)
                AsOfStamp(date: c1.dataAt)
            }
            .frame(width: 112, alignment: .leading)

            VStack(spacing: 8) {
                ForEach(c1.nodes) { node in
                    HStack(spacing: 8) {
                        Text(node.label)
                            .font(NativeStyle.small())
                            .foregroundStyle(.primary)
                            .lineLimit(1)
                            .frame(width: 42, alignment: .leading)
                        MeterBar(value: node.gpuPct.map { $0 / 100 },
                                 tint: NativeStyle.utilisation(node.gpuPct))
                        Text(node.tempC.map { "\(Int($0.rounded()))°" } ?? "—")
                            .font(NativeStyle.number())
                            .foregroundStyle(ClusterC1Ink.temperature(node.tempC))
                            .frame(width: 34, alignment: .trailing)
                    }
                }
                Spacer(minLength: 0)
            }
            .frame(maxWidth: .infinity)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
        .stale(c1.stale)
    }
}

// MARK: - Large

struct LargeClusterWidget: View {
    let entry: ClusterEntry

    var body: some View {
        if !entry.configured || entry.paused {
            ClusterC1Gate(entry: entry)
        } else {
            largeBody
        }
    }

    private var largeBody: some View {
        let c1 = entry.c1
        return VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline) {
                Text("Cluster · \(c1.headline)")
                    .font(NativeStyle.label())
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                Spacer(minLength: 0)
                AsOfStamp(date: c1.dataAt)
            }

            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text(c1.tokPerSec.map { "\(Int($0.rounded()))" } ?? "—")
                    .font(NativeStyle.hero(34))
                    .monospacedDigit()
                    .lineLimit(1)
                Text("tok/s now")
                    .font(NativeStyle.sub())
                    .foregroundStyle(.secondary)
            }

            ClusterThroughputChart(points: entry.throughput)

            VStack(alignment: .leading, spacing: 5) {
                Grid(alignment: .leading, horizontalSpacing: 8, verticalSpacing: 5) {
                    GridRow {
                        ClusterC1Label(text: "node")
                        ClusterC1Label(text: "util")
                        ClusterC1Label(text: "temp")
                        ClusterC1Label(text: "power")
                        ClusterC1Label(text: "tok/s")
                    }
                    ForEach(c1.nodes) { node in
                        GridRow {
                            Text(node.isOrchestrator ? "\(node.label)◆" : node.label)
                                .font(NativeStyle.small())
                                .lineLimit(1)
                            MeterBar(value: node.gpuPct.map { $0 / 100 },
                                     tint: NativeStyle.utilisation(node.gpuPct))
                            Text(node.tempC.map { "\(Int($0.rounded()))°" } ?? "—")
                                .font(NativeStyle.number())
                                .foregroundStyle(ClusterC1Ink.temperature(node.tempC))
                            Text(node.powerW.map { "\(Int($0.rounded())) W" } ?? "—")
                                .font(NativeStyle.number())
                            Text(node.genTps.map { "\(Int($0.rounded()))" } ?? "—")
                                .font(NativeStyle.number())
                        }
                    }
                }
            }

            Spacer(minLength: 0)
            HStack(alignment: .firstTextBaseline) {
                Text(c1.modelLine)
                    .font(NativeStyle.small())
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                Spacer(minLength: 0)
                Text(c1.daemonText)
                    .font(NativeStyle.small())
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
        .stale(c1.stale)
    }
}

struct ExtraLargeClusterWidget: View {
    let entry: ClusterEntry

    var body: some View {
        LargeClusterWidget(entry: entry)
    }
}

/// The 2 h total-throughput area chart. Points are sums of the engine series
/// at the recorder's own timestamps; a missing point stays a gap.
private struct ClusterThroughputChart: View {
    let points: [ClusterThroughputPoint]

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            if points.count >= 2 {
                GeometryReader { geometry in
                    let maxValue = max(points.map(\.value).max() ?? 1, 1)
                    let first = points.first?.t ?? 0
                    let last = points.last?.t ?? 1
                    let span = max(last - first, 1)
                    ZStack {
                        chartPath(in: geometry.size, maxValue: maxValue,
                                  first: first, span: span, closed: true)
                            .fill(NativeStyle.accent.opacity(0.16))
                        chartPath(in: geometry.size, maxValue: maxValue,
                                  first: first, span: span, closed: false)
                            .stroke(NativeStyle.accent, lineWidth: 1.6)
                    }
                }
                .frame(height: 58)
                HStack {
                    Text("2 h ago").font(NativeStyle.small()).foregroundStyle(.secondary)
                    Spacer(minLength: 0)
                    Text("now").font(NativeStyle.small()).foregroundStyle(.secondary)
                }
            } else {
                Text("No 2 h chart")
                    .font(NativeStyle.small())
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, minHeight: 58, alignment: .center)
            }
        }
    }

    private func chartPath(in size: CGSize, maxValue: Double, first: TimeInterval,
                           span: TimeInterval, closed: Bool) -> Path {
        var path = Path()
        for (index, point) in points.enumerated() {
            let x = CGFloat((point.t - first) / span) * size.width
            let y = size.height - CGFloat(point.value / maxValue) * (size.height - 2) - 1
            if index == 0 {
                if closed { path.move(to: CGPoint(x: x, y: size.height)) }
                path.addLine(to: CGPoint(x: x, y: y))
            } else {
                path.addLine(to: CGPoint(x: x, y: y))
            }
        }
        if closed, let lastX = points.last.map({ CGFloat((($0.t - first) / span)) * size.width }) {
            path.addLine(to: CGPoint(x: lastX, y: size.height))
            path.closeSubpath()
        }
        return path
    }
}

// MARK: - Lock Screen accessories (kept; redesigned by a later card)

/// The Lock Screen ring: the fleet's GPU load as a capacity gauge, with the
/// wattage underneath. Both strings come from the entry's text model.
struct CircularClusterWidget: View {
    let entry: ClusterEntry

    var body: some View {
        let text = entry.text(for: .circular)
        if let fill = entry.lockScreenGaugeValue {
            VStack(spacing: 0) {
                Gauge(value: min(max(fill, 0), 100), in: 0...100) {
                    Text(CLILockScreenCopy.gaugeLabel)
                } currentValueLabel: {
                    Text(text.headline)
                        .font(NativeStyle.number(11))
                }
                .gaugeStyle(.accessoryCircularCapacity)
                .tint(.primary)
                if let watts = text.detail {
                    Text(watts)
                        .font(NativeStyle.small())
                        .foregroundStyle(.secondary)
                }
            }
            .accessibilityLabel(entry.lockScreenVoice ?? text.headline)
        } else {
            Text(text.headline)
                .font(NativeStyle.number(9))
                .foregroundStyle(.secondary)
                .minimumScaleFactor(0.6)
                .multilineTextAlignment(.center)
                .accessibilityLabel(entry.lockScreenVoice ?? text.headline)
        }
    }
}

/// The Lock Screen row: the CLI's four numbers, then the queue/host coverage,
/// then the age or failure reason. The fallback's node rows stay.
struct RectangularClusterWidget: View {
    let entry: ClusterEntry

    var body: some View {
        let text = entry.text(for: .rectangular)
        VStack(alignment: .leading, spacing: 2) {
            Text(text.headline)
                .font(NativeStyle.number(12))
                .foregroundStyle(.primary)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
            if let detail = text.detail {
                Text(detail)
                    .font(NativeStyle.number(10, weight: .medium))
                    .foregroundStyle(.primary)
                    .lineLimit(1)
                    .minimumScaleFactor(0.7)
            }
            ForEach(Array(text.rows.prefix(1).enumerated()), id: \.offset) { _, row in
                Text(row.title + (row.detail.map { " · \($0)" } ?? ""))
                    .font(NativeStyle.small())
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .minimumScaleFactor(0.7)
            }
            if let footer = text.footer {
                Text(footer)
                    .font(NativeStyle.small())
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            Spacer(minLength: 0)
        }
        .accessibilityLabel(entry.lockScreenVoice ?? text.headline)
    }
}

/// One line: headline and detail, nothing truncated into soup.
struct InlineClusterWidget: View {
    let entry: ClusterEntry

    var body: some View {
        Text(entry.inlineLine)
            .font(NativeStyle.small())
            .accessibilityLabel(entry.inlineLine)
    }
}
