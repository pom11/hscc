import Charts
import SwiftUI
import WidgetKit

// ---------------------------------------------------------------------------
// The history graph widget's marks and its four family bodies (t_f4551339).
//
// WHAT THIS FILE OWNS: layout and colour. Nothing else. Every number on it came
// from `ClusterGraphReading`/`ClusterHistorySeries` (already reduced, already
// formatted), every verdict from `ClusterMetricLevel` (`t_61ed3f5e`), and every
// font and colour from the operator's stored `WidgetStyle`
// (`widget_style_check.sh` enforces the file-wide half, and it refuses this
// file's own comments spelling the banned patterns — so the rule is stated here
// without naming them).
//
// THREE RULES THIS FILE MUST KEEP.
//
//   1. NO HARD-CODED FRAME, NO HARD-CODED LABEL COUNT. The measured width reaches
//      every chart as a PARAMETER (`ClusterGraphCanvas` measures it once and hands
//      it down), and the same integer is what the reduction was asked for — so the
//      axis can never state a time density its buckets do not have. The macOS
//      menu-bar dropdown (t_be4e1a48) passes its own width
//      (`360 − 2 × Theme.Spacing.lg`) and its own label count to `ClusterGraphMarks`
//      and embeds these same marks rather than wrapping a copy of them.
//   2. ONE GATE STEP PER BODY. Every family body that could draw live data checks
//      BOTH gate states in one step, so no family can forget the second and reach a
//      number on a paused or unconfigured surface. The copy for both states is owned
//      by the entry (`gateLine`), once.
//   3. A GAP IS A GAP. Each segment the reduction produced is drawn as its own
//      `LineMark` with its own identity, so a window with no reading stays an empty
//      stretch of axis instead of becoming a straight line between the two nearest
//      numbers. A line is built only from points that carry a value, so `null` is
//      never drawn as `0`.
//
// The root paints its background through `WidgetSurfaceBackground` and applies
// `widgetRenderingAdaptation(style:)`, so in accented/vibrant rendering every
// colour below becomes the system's ink while the fonts, the scale and the two
// toggles stay the operator's.
// ---------------------------------------------------------------------------

// MARK: - The measured canvas

/// Measures the chart area ONCE and hands the width and the label count down.
///
/// This is the only place a size is measured, and the pair it produces is the
/// pair every reduction and every axis in the widget is built from.
struct ClusterGraphCanvas<Content: View>: View {
    let family: WidgetFamily
    @ViewBuilder let content: (Int, Int) -> Content

    var body: some View {
        GeometryReader { proxy in
            let width = ClusterGraphAxisRule.pixelColumns(measuredWidth: proxy.size.width)
            // A measurement of nothing (a widget still laying out) falls back to
            // the size's own floor rather than to 0 columns, which would flatten a
            // real series into two points — see `clusterGraphUnmeasuredPixelWidth`.
            let columns = proxy.size.width > 0
                ? width
                : family.clusterGraphUnmeasuredPixelWidth
            content(columns, ClusterGraphAxisRule.labelCount(pixelWidth: columns))
                .frame(width: proxy.size.width, height: proxy.size.height, alignment: .topLeading)
        }
    }
}

// MARK: - The marks

/// One mini-chart: one unit, the lines that share it, its own labelled axes, and
/// (for temperatures) its threshold bands.
struct ClusterGraphMarks: View {
    let mini: ClusterGraphMini
    let pixelWidth: Int
    let axisLabelCount: Int
    let window: ClusterGraphWindow
    /// Whether this mini prints its own x labels. Minis inside one panel row share
    /// a time axis, so only the row that owns the axis prints them; the y axis is
    /// always printed, because a line without a scale is a shape and not a
    /// measurement.
    let showsXAxis: Bool
    @Environment(\.widgetStyle) private var style

    init(mini: ClusterGraphMini,
         pixelWidth: Int,
         axisLabelCount: Int,
         window: ClusterGraphWindow,
         showsXAxis: Bool = true) {
        self.mini = mini
        self.pixelWidth = pixelWidth
        self.axisLabelCount = axisLabelCount
        self.window = window
        self.showsXAxis = showsXAxis
    }

    var body: some View {
        Chart {
            ForEach(bands) { band in
                RectangleMark(yStart: .value("from", band.from),
                              yEnd: .value("to", band.to))
                    .foregroundStyle(ink(band.level).opacity(0.16))
            }
            ForEach(bands) { band in
                RuleMark(y: .value("threshold", band.from))
                    .foregroundStyle(ink(band.level).opacity(0.7))
                    .lineStyle(StrokeStyle(lineWidth: 0.5, dash: [2, 2]))
            }
            ForEach(segments) { segment in
                ForEach(segment.points) { point in
                    LineMark(x: .value("time", point.date),
                             y: .value(mini.axisLabel, point.value),
                             series: .value("line", segment.name))
                        .foregroundStyle(segment.ink)
                        .lineStyle(StrokeStyle(lineWidth: 1.2))
                }
            }
        }
        .chartYScale(domain: mini.axisFloor...mini.axisCeiling)
        .chartYAxis {
            AxisMarks(preset: .extended, position: .leading) { _ in
                AxisGridLine().foregroundStyle(style.secondaryTextColor.opacity(0.25))
                AxisValueLabel()
                    .font(style.font(size: 7))
                    .foregroundStyle(style.secondaryTextColor)
            }
        }
        .chartXAxis {
            if showsXAxis {
                AxisMarks(values: .automatic(desiredCount: axisLabelCount)) { _ in
                    AxisGridLine().foregroundStyle(style.secondaryTextColor.opacity(0.18))
                    AxisValueLabel(format: ClusterGraphAxisFormat.for(window: window))
                        .font(style.font(size: 7))
                        .foregroundStyle(style.secondaryTextColor)
                }
            }
        }
    }

    /// The bands behind a temperature chart, and none behind any other unit:
    /// `ClusterGraphBandRule.verdict(for:)` is the record of which units have a
    /// declared band, and a band painted for a threshold nobody declared is
    /// decoration presented as a warning.
    private var bands: [ClusterGraphBand] {
        mini.unit == .temperature
            ? ClusterGraphBandRule.temperatureBands(from: mini.axisFloor, top: mini.axisCeiling)
            : []
    }

    private func ink(_ level: ClusterMetricLevel) -> Color {
        ClusterGraphInk.color(for: .verdict(level), style: style)
    }

    /// One drawable polyline: one segment of one line, named stably and coloured
    /// by its own verdict.
    private struct Segment: Identifiable {
        let id: Int
        let name: String
        let ink: Color
        let points: [PlotPoint]
    }

    /// One vertex. The id is positional because the same timestamp can legitimately
    /// appear twice in one reduction (a pixel column's minimum AND maximum are both
    /// real points; over a fleet, two hosts can report the same second), so the
    /// timestamp cannot identify a point.
    private struct PlotPoint: Identifiable {
        let id: Int
        let date: Date
        let value: Double
    }

    private var segments: [Segment] {
        var out: [Segment] = []
        var index = 0
        for line in mini.lines {
            for (offset, segment) in line.segments.enumerated() {
                var points: [PlotPoint] = []
                for sample in segment {
                    guard let value = sample.value else { continue }
                    points.append(PlotPoint(id: index,
                                            date: Date(timeIntervalSince1970: sample.t),
                                            value: value))
                    index += 1
                }
                // A single point is not a line: drawing one would be a dot that
                // reads as a measurement of a trend.
                guard points.count > 1 else { continue }
                out.append(Segment(id: out.count,
                                   name: "\(line.metric.rawValue)#\(line.identity)#\(offset)",
                                   ink: ClusterGraphInk.color(for: line.tint, style: style),
                                   points: points))
            }
        }
        return out
    }
}

/// Where a colour comes from, so a line, a band and a caption cannot disagree
/// about what `warn` looks like.
enum ClusterGraphInk {
    static func color(for tint: ClusterGraphTint, style: WidgetStyle) -> Color {
        switch tint {
        case .verdict(let level):
            return level.color(style)
        case .unthresholded:
            // The accent says "this is a line". A status colour would say "this
            // value is fine", and no declared rule was consulted to decide it.
            return style.accentColor
        }
    }
}

/// Which part of a timestamp a tick prints at this window.
/// `automatic(desiredCount:)` decides WHERE the ticks go; this decides what one
/// says — and a 7-day axis whose ticks all read "14:00" is not a time axis.
enum ClusterGraphAxisFormat {
    static func `for`(window: ClusterGraphWindow) -> Date.FormatStyle {
        switch window {
        case .fifteenMinutes: return .dateTime.hour().minute().second()
        case .oneHour, .sixHours, .twentyFourHours: return .dateTime.hour().minute()
        case .sevenDays: return .dateTime.day().month(.abbreviated)
        }
    }
}

// MARK: - Small: one metric, one line, current / min / max

/// `systemSmall`: ONE metric sparkline with its current value and the whole
/// series' min and max. The min/max are the reduction's TRUE extremes, not the
/// drawn points' — the caption is a claim about the data, not about which points
/// survived. The resolution line is what tells the operator whether this line
/// moves every 2 seconds or every 5 minutes.
struct ClusterGraphSmallWidget: View {
    @Environment(\.widgetStyle) private var style
    let entry: ClusterGraphEntry
    let pixelWidth: Int
    let axisLabelCount: Int

    var body: some View {
        // ONE step for BOTH gate states: an unconfigured device gets the setup
        // invite, a paused one the paused copy, and neither draws a number
        // (t_5c554c5b / t_5f246177).
        if !entry.configured || entry.paused {
            ClusterGraphGateLine(line: entry.gateLine)
        } else if let line = entry.sparkline(pixelWidth: pixelWidth) {
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 3) {
                    Image(systemName: "chart.xyaxis.line").widgetIcon(style)
                    Text(verbatim: line.metric.label)
                        .font(style.font(size: 9, weight: .semibold))
                }
                .foregroundStyle(style.secondaryTextColor)
                .lineLimit(1)
                .widgetHeader(style)
                Text(verbatim: ClusterGraphCaption.value(line.current, metric: line.metric))
                    .font(style.font(size: 22, weight: .bold))
                    .foregroundStyle(ClusterGraphInk.color(for: line.tint, style: style))
                    .minimumScaleFactor(0.5)
                    .lineLimit(1)
                ClusterGraphMarks(mini: entry.mini(for: line),
                                  pixelWidth: pixelWidth,
                                  axisLabelCount: axisLabelCount,
                                  window: entry.window,
                                  showsXAxis: false)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                HStack(spacing: 4) {
                    caption("min", line.minimum, line.metric)
                    caption("max", line.maximum, line.metric)
                }
                .font(style.font(size: 9))
                .foregroundStyle(style.secondaryTextColor)
                Text(verbatim: entry.resolutionLine)
                    .font(style.font(size: 8))
                    .foregroundStyle(style.secondaryTextColor)
                    .lineLimit(1)
            }
        } else {
            ClusterGraphEmptyView(line: entry.emptyLine, style: style)
        }
    }

    private func caption(_ word: String,
                         _ value: Double?,
                         _ metric: ClusterHistoryMetric) -> some View {
        Text(verbatim: "\(word) \(ClusterGraphCaption.value(value, metric: metric))")
            .lineLimit(1)
            .minimumScaleFactor(0.7)
    }
}

// MARK: - Medium: one node, all temps plus GPU%, each with its current value

/// `systemMedium`: one node's temperatures plus GPU utilisation, every line with
/// its current value beside it, and the threshold bands behind the temperatures.
///
/// The temperatures draw on the °C mini and the utilisation on the % mini, each
/// with its own labelled y axis: a percentage and a temperature sharing one axis
/// would make one of the two heights a lie.
struct ClusterGraphMediumWidget: View {
    @Environment(\.widgetStyle) private var style
    let entry: ClusterGraphEntry
    let pixelWidth: Int
    let axisLabelCount: Int

    var body: some View {
        if !entry.configured || entry.paused {
            ClusterGraphGateLine(line: entry.gateLine)
        } else if !entry.minis(pixelWidth: pixelWidth).isEmpty {
            VStack(alignment: .leading, spacing: 3) {
                header
                ForEach(entry.minis(pixelWidth: pixelWidth)) { mini in
                    HStack(spacing: 4) {
                        ClusterGraphMarks(mini: mini,
                                          pixelWidth: pixelWidth,
                                          axisLabelCount: axisLabelCount,
                                          window: entry.window,
                                          showsXAxis: true)
                            .frame(maxWidth: .infinity)
                            .frame(height: 44)
                        VStack(alignment: .trailing, spacing: 2) {
                            ForEach(mini.lines, id: \.identity) { line in
                                currentCell(line)
                            }
                        }
                    }
                }
                Text(verbatim: entry.footerLine)
                    .font(style.font(size: 8))
                    .foregroundStyle(style.secondaryTextColor)
                    .lineLimit(1)
            }
        } else {
            ClusterGraphEmptyView(line: entry.emptyLine, style: style)
        }
    }

    private var header: some View {
        HStack(spacing: 3) {
            Image(systemName: "chart.xyaxis.line").widgetIcon(style)
            Text(verbatim: entry.scopeLine)
                .font(style.font(size: 10, weight: .semibold))
        }
        .foregroundStyle(style.secondaryTextColor)
        .lineLimit(1)
        .widgetHeader(style)
    }

    private func currentCell(_ line: ClusterGraphLine) -> some View {
        VStack(alignment: .trailing, spacing: 0) {
            Text(verbatim: ClusterGraphCaption.value(line.current, metric: line.metric))
                .font(style.font(size: 11, weight: .semibold))
                .foregroundStyle(ClusterGraphInk.color(for: line.tint, style: style))
            Text(verbatim: line.metric.label)
                .font(style.font(size: 7))
                .foregroundStyle(style.secondaryTextColor)
        }
        .lineLimit(1)
        .minimumScaleFactor(0.7)
    }
}

// MARK: - Large / extra-large: small multiples, one panel per node

/// `systemLarge` and `systemExtraLarge`: one panel per node, each panel a row of
/// small multiples grouped by unit — every metric the card names is drawn, each on
/// an axis whose unit fits it — with axis labels and a legend.
struct ClusterGraphPanelWidget: View {
    @Environment(\.widgetStyle) private var style
    let entry: ClusterGraphEntry
    let pixelWidth: Int
    let axisLabelCount: Int
    /// Read from the family so `systemLarge` and `systemExtraLarge` each have their
    /// own arm in the dispatch and neither reaches the other's layout through a
    /// `default:`.
    let family: WidgetFamily

    var body: some View {
        if !entry.configured || entry.paused {
            ClusterGraphGateLine(line: entry.gateLine)
        } else if !entry.panels(pixelWidth: pixelWidth).isEmpty {
            let panels = entry.panels(pixelWidth: pixelWidth)
            VStack(alignment: .leading, spacing: 4) {
                header
                ForEach(panels, id: \.host) { panel in
                    ClusterGraphPanelView(panel: panel,
                                          entry: entry,
                                          pixelWidth: pixelWidth,
                                          axisLabelCount: axisLabelCount,
                                          miniHeight: family.clusterGraphDensity?.miniHeight ?? 46)
                }
                legend(panels: panels)
                Text(verbatim: entry.footerLine)
                    .font(style.font(size: 8))
                    .foregroundStyle(style.secondaryTextColor)
                    .lineLimit(1)
            }
        } else {
            ClusterGraphEmptyView(line: entry.emptyLine, style: style)
        }
    }

    private var header: some View {
        HStack(spacing: 3) {
            Image(systemName: "chart.xyaxis.line").widgetIcon(style)
            Text(verbatim: entry.scopeLine)
                .font(style.font(size: 10, weight: .semibold))
        }
        .foregroundStyle(style.secondaryTextColor)
        .lineLimit(1)
        .widgetHeader(style)
    }

    /// Every metric this chart drew, its colour and its name. A colour nothing
    /// names is decoration, so the legend is part of the density requirement
    /// rather than garnish — and it is built from the lines that were actually
    /// drawn, so it can never advertise a metric the payload did not carry.
    private func legend(panels: [ClusterGraphPanel]) -> some View {
        let entries = ClusterGraphReading.legend(for: panels.flatMap(\.lines))
        // Two rows of four, because a legend that wraps to five rows costs the
        // panels their height. Past the budget the rest is dropped and the count
        // says so rather than pretending the legend is complete.
        let shown = Array(entries.prefix(8))
        return VStack(alignment: .leading, spacing: 1) {
            ForEach(Array(stride(from: 0, to: shown.count, by: 4)), id: \.self) { row in
                HStack(spacing: 5) {
                    ForEach(Array(shown[row..<min(row + 4, shown.count)]), id: \.metric) { item in
                        HStack(spacing: 2) {
                            Circle()
                                .fill(ClusterGraphInk.color(for: item.tint, style: style))
                                .frame(width: 4, height: 4)
                            Text(verbatim: item.metric.label)
                                .font(style.font(size: 7))
                        }
                        .foregroundStyle(style.secondaryTextColor)
                        .lineLimit(1)
                    }
                }
            }
            if entries.count > shown.count {
                Text(verbatim: "+\(entries.count - shown.count) more")
                    .font(style.font(size: 7))
                    .foregroundStyle(style.secondaryTextColor)
            }
        }
    }
}

/// One node's row of minis.
private struct ClusterGraphPanelView: View {
    @Environment(\.widgetStyle) private var style
    let panel: ClusterGraphPanel
    let entry: ClusterGraphEntry
    let pixelWidth: Int
    let axisLabelCount: Int
    let miniHeight: CGFloat

    var body: some View {
        let minis = ClusterGraphReading.minis(from: panel.lines)
        VStack(alignment: .leading, spacing: 1) {
            HStack(spacing: 3) {
                Text(verbatim: panel.displayLabel)
                    .font(style.font(size: 9, weight: .semibold))
                    .foregroundStyle(style.primaryTextColor)
                Text(verbatim: minis.isEmpty ? "no samples" : axisSummary(minis))
                    .font(style.font(size: 7))
                    .foregroundStyle(style.secondaryTextColor)
            }
            .lineLimit(1)
            if minis.isEmpty {
                Text(verbatim: entry.emptyLine ?? "no stored samples")
                    .font(style.font(size: 8))
                    .foregroundStyle(style.secondaryTextColor)
                    .lineLimit(1)
            } else {
                HStack(spacing: 3) {
                    ForEach(minis) { mini in
                        VStack(spacing: 0) {
                            ClusterGraphMarks(mini: mini,
                                              pixelWidth: pixelWidth,
                                              axisLabelCount: axisLabelCount,
                                              window: entry.window,
                                              showsXAxis: false)
                                .frame(height: miniHeight)
                            Text(verbatim: mini.axisLabel)
                                .font(style.font(size: 6))
                                .foregroundStyle(style.secondaryTextColor)
                        }
                        .frame(maxWidth: .infinity)
                    }
                }
            }
        }
    }

    /// The units this panel draws, spelled out — the legend's unit column for the
    /// reader who wants to know what the four minis are before they read the axes.
    private func axisSummary(_ minis: [ClusterGraphMini]) -> String {
        minis.map(\.axisLabel).filter { !$0.isEmpty }.joined(separator: " ")
    }
}

// MARK: - Shared chrome

/// The one line the two gate states draw. The copy belongs to the entry.
struct ClusterGraphGateLine: View {
    @Environment(\.widgetStyle) private var style
    let line: String

    var body: some View {
        VStack(spacing: 4) {
            Image(systemName: "gearshape")
                .widgetIcon(style)
                .foregroundStyle(style.secondaryTextColor)
            Text(verbatim: line)
                .font(style.font(size: 11, weight: .medium))
                .foregroundStyle(style.secondaryTextColor)
                .multilineTextAlignment(.center)
                .widgetHeader(style)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

/// Nothing to draw, and the payload's own sentence saying why. An empty chart is
/// never drawn as a flat line at zero, and "nothing stored" is never "the fleet is
/// quiet".
struct ClusterGraphEmptyView: View {
    let line: String?
    let style: WidgetStyle

    var body: some View {
        VStack(spacing: 3) {
            Image(systemName: "chart.xyaxis.line")
                .widgetIcon(style)
                .foregroundStyle(style.secondaryTextColor)
            Text(verbatim: line ?? "no stored samples")
                .font(style.font(size: 10))
                .foregroundStyle(style.secondaryTextColor)
                .multilineTextAlignment(.center)
                .widgetHeader(style)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

// MARK: - The dispatch

/// The widget root: which body a family gets, and the ONE place a width is
/// measured.
///
/// Every declared family has its OWN arm and `default:` serves only a size the
/// widget does not offer — a declared family reaching a `default:` is exactly how
/// t_b3ae3809's undeclared `systemLarge` came to draw the medium layout, so
/// `cluster_graph_check.sh` iterates `WidgetFamily.clusterGraphFamilies` and
/// requires an arm for each name.
struct ClusterGraphWidgetView: View {
    @Environment(\.widgetStyle) private var style
    let entry: ClusterGraphEntry

    var body: some View {
        ClusterGraphCanvas(family: entry.family) { pixelWidth, axisLabelCount in
            Group {
                switch entry.family {
                case .systemSmall:
                    ClusterGraphSmallWidget(entry: entry,
                                            pixelWidth: pixelWidth,
                                            axisLabelCount: axisLabelCount)
                case .systemMedium:
                    ClusterGraphMediumWidget(entry: entry,
                                             pixelWidth: pixelWidth,
                                             axisLabelCount: axisLabelCount)
                case .systemLarge:
                    ClusterGraphPanelWidget(entry: entry,
                                            pixelWidth: pixelWidth,
                                            axisLabelCount: axisLabelCount,
                                            family: .systemLarge)
                case .systemExtraLarge:
                    ClusterGraphPanelWidget(entry: entry,
                                            pixelWidth: pixelWidth,
                                            axisLabelCount: axisLabelCount,
                                            family: .systemExtraLarge)
                default:
                    ClusterGraphSmallWidget(entry: entry,
                                            pixelWidth: pixelWidth,
                                            axisLabelCount: axisLabelCount)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        }
    }
}
