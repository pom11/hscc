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
// The Lock Screen accessory bodies below are the L2 Gauges pick: a
// 0...90 °C hottest-node ring, a "CLUSTER · N TOK/S" row with four node temps,
// and "4/4 up · N tok/s" inline. They read the same C1 projection as the Home
// Screen tile, so no family can disagree about a node, a temperature or tok/s.
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

// MARK: - Lock Screen accessories — L2 Gauges (2026-11-20)

/// The L2 ring: the hottest node's temperature on a 0...90 °C gauge, with the
/// node's short label inside the ring. No reading is an em dash, never 0 °C.
struct CircularClusterWidget: View {
    let entry: ClusterEntry

    var body: some View {
        let c1 = entry.c1
        if !entry.configured || entry.paused {
            Text(entry.shortGateLabel)
                .font(NativeStyle.small())
                .foregroundStyle(.secondary)
                .minimumScaleFactor(0.6)
                .multilineTextAlignment(.center)
        } else if let node = c1.hottestNode, let celsius = node.tempC,
                  let fill = c1.hottestGaugeValue {
            L2Gauge(value: fill, range: 0...90,
                    tint: NativeStyle.temperature(celsius),
                    valueText: "\(Int(celsius.rounded()))°",
                    caption: node.label,
                    label: "Hottest \(node.label)")
                .stale(c1.stale)
        } else if let failure = entry.failure {
            Text(failure.shortLabel)
                .font(NativeStyle.small())
                .foregroundStyle(.primary)
                .minimumScaleFactor(0.6)
                .multilineTextAlignment(.center)
        } else {
            VStack(spacing: 0) {
                Text("—")
                    .font(NativeStyle.number(17, weight: .bold))
                Text("temp")
                    .font(NativeStyle.small())
                    .foregroundStyle(.secondary)
            }
            .accessibilityLabel("No node reported a temperature")
        }
    }
}

/// The L2 row: "CLUSTER · N TOK/S", then the four node temperatures in one row.
struct RectangularClusterWidget: View {
    let entry: ClusterEntry

    var body: some View {
        let c1 = entry.c1
        if !entry.configured || entry.paused {
            Text(entry.shortGateLabel)
                .font(NativeStyle.small())
                .foregroundStyle(.secondary)
        } else if c1.nodes.isEmpty, let failure = entry.failure {
            VStack(alignment: .leading, spacing: 1) {
                Text("CLUSTER")
                    .font(NativeStyle.label())
                    .foregroundStyle(.secondary)
                Text(failure.shortLabel)
                    .font(NativeStyle.sub())
                    .foregroundStyle(.primary)
            }
        } else {
            VStack(alignment: .leading, spacing: 4) {
                Text(c1.rectangularHeadline)
                    .font(NativeStyle.label())
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .minimumScaleFactor(0.7)
                HStack(spacing: 4) {
                    ForEach(c1.nodes.prefix(4)) { node in
                        Text(node.tempC.map { "\(Int($0.rounded()))°" } ?? "—")
                            .font(NativeStyle.number(15, weight: .bold))
                            .foregroundStyle(NativeStyle.temperature(node.tempC))
                            .lineLimit(1)
                            .minimumScaleFactor(0.7)
                            .frame(maxWidth: .infinity)
                    }
                }
                .widgetAccentable()
                Spacer(minLength: 0)
            }
            .stale(c1.stale)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("Cluster, \(c1.tokText), node temperatures "
                                + c1.rectangularTemps.joined(separator: ", "))
        }
    }
}

/// One line: "4/4 up · N tok/s".
struct InlineClusterWidget: View {
    let entry: ClusterEntry

    var body: some View {
        let c1 = entry.c1
        if !entry.configured || entry.paused {
            Text(entry.shortGateLabel)
                .font(NativeStyle.small())
        } else if let failure = entry.failure {
            Text(failure.shortLabel)
                .font(NativeStyle.small())
        } else {
            Text(c1.inlineL2)
                .font(NativeStyle.small())
                .monospacedDigit()
                .accessibilityLabel(c1.inlineL2)
        }
    }
}
