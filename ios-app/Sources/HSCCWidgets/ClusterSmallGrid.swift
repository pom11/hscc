import Foundation

// ===========================================================================
// The systemSmall grid — the data half of the densest tile the app draws.
//
// A small tile is roughly 130x130 pt of usable space after the container's own
// padding. The card this file exists for (t_9e110d88) calls a small widget that
// shows one number a failure of the design, so this tile draws a GRID: up to
// four hosts x four CLI columns (GPU%, GPU Temp, CPU Temp, GPU Power) plus the
// serving totals underneath. 16 host cells + 2 totals, every one of them a
// string the CLI itself printed.
//
// THREE RULES THIS FILE ENFORCES BY CONSTRUCTION.
//
//   1. No number, unit or rounding is composed here. Every cell string is a
//      cell of `CLIMonitorHostRow` — the view-model's CLI-parity row — looked
//      up BY THE CLI'S OWN COLUMN NAME (`CLIMonitorHostRow.columns`), never by
//      a hand-picked field. Rename a column in the view-model and the lookup
//      falls back to `-` and `model_decode_check` goes red; it cannot silently
//      mislabel a value, which is what a positional pick would do.
//   2. Colour comes back as `ClusterMetricLevel`, never as a `Color`. That is
//      what keeps this file Foundation-only, and a Foundation-only file is what
//      lets `model_decode_check.sh` compile and RUN the exact bytes the shipped
//      tile runs (the view half in `ClusterSmallGridView.swift` imports
//      SwiftUI and is invisible to that harness).
//   3. "Not told" is nil and renders `-`, never 0 (§6 rule 1). A host that
//      never sampled has no temperature to paint; a serving section the route
//      did not send has no queue depth.
//
// THE TREND (t_2681d3de).
//
// A bar under the GPU% digit says one thing: what that host read on the last
// refresh. The card this tile follows asked for a real trend, and the recorder
// answers one — `GET /v1/cluster/history` (see `ClusterHistorySeries.swift`).
// `Trend` is that series reduced to the cell's track: the SAME pixel-column
// downsampler every other history surface uses, so the reduced polyline keeps
// each column's min and max and therefore the series' true extremes. The join
// from tile row to series is by the payload's own `host` identity, used as a
// key only — an address never becomes a drawn string (the harness scans the
// trend's spoken text for one exactly as it scans every cell). A host the
// recorder cannot answer for has NO trend, and the view falls back to the
// single-sample bar rather than drawing a flat line off one reading.
//
// THE ONE THRESHOLD THAT IS NOT THE VIEW-MODEL'S.
//
// GPU utilisation is the only column here the view-model does not classify
// (it classifies both temps, GPU power, CPU% and RAM%). Its band is the
// widget's own existing one — `ClusterMetricHeat.percent(_:isCPU: false)` in
// `ClusterWidgetMetrics.swift`, warn above 90 / hot above 95 — restated here as
// the two constants below because a Foundation-only file cannot read a type
// that imports WidgetKit. `cluster_widget_check.sh` pins the pair against that
// rule, so the two cannot drift apart silently. Note the boundary convention
// differs by one comparison: `ClusterMetricHeat` uses `>` and
// `ClusterMetricLevel.level` uses `>=`, so a host sitting at exactly 90 reads
// warn here and calm there. Both are display bands, not hardware specs, and the
// number is always printed beside the colour.
// ===========================================================================

enum ClusterSmallGrid {
    /// How many hosts fit in the tile. Beyond this the tile counts what it
    /// dropped instead of shrinking the type past legible.
    static let maxHosts = 4

    /// GPU utilisation band — see the header: the same band the cluster tile's
    /// node dot already uses, restated for a Foundation-only caller.
    static let gpuUtilWarn: Double = 90
    static let gpuUtilCritical: Double = 95

    /// The label for the column the host names sit in — read out of the
    /// view-model's own column list, so the tile has exactly one vocabulary and
    /// a renamed CLI column reddens `model_decode_check` instead of quietly
    /// leaving a view with a hand-typed heading.
    static var hostColumnLabel: String { CLIMonitorHostRow.columns.first ?? "Host" }

    /// The serving columns the totals line labels itself with, spelled as the
    /// CLI spells them (asserted against `serving_table`'s column list).
    static let runColumnLabel = "Run"
    static let waitColumnLabel = "Wait"

    /// The one number-to-text rule of this tile: a count that was not told gets
    /// the CLI's dash, never a `0` that reads as a measurement (§6 rule 1).
    /// Every count on the surface goes through here so the dash has one owner.
    static func countText(_ value: Int?) -> String {
        value.map(String.init) ?? CLIMonitorFormat.dash
    }

    /// The four columns of the grid, left to right. `CaseIterable` order IS
    /// the visual order, so the harness can walk the row without restating it.
    enum Column: String, CaseIterable, Identifiable {
        case gpuUtil
        case gpuTemp
        case cpuTemp
        case gpuPower

        var id: String { rawValue }

        /// The header text and the parity key: the CLI's own column name for
        /// this cell. The value is read out of the same row at this name's
        /// index, so label and value cannot drift apart.
        var cliColumn: String {
            switch self {
            case .gpuUtil: return "GPU%"
            case .gpuTemp: return "GPU Temp"
            case .cpuTemp: return "CPU Temp"
            case .gpuPower: return "GPU Power"
            }
        }

        /// Where this column sits in `host_table`'s row, per the CLI's own
        /// `columns` list. Nil means the view-model renamed the column — the
        /// cell then prints `-` rather than whatever happens to sit at the old
        /// index.
        var hostTableIndex: Int? {
            CLIMonitorHostRow.columns.firstIndex(of: cliColumn)
        }

        /// The cell text, straight from the CLI-parity row.
        func cellText(in row: CLIMonitorHostRow) -> String {
            guard let index = hostTableIndex, row.cells.indices.contains(index) else {
                return CLIMonitorFormat.dash
            }
            return row.cells[index]
        }

        /// The view-model's threshold verdict for this column.
        func level(of row: CLIMonitorHostRow) -> ClusterMetricLevel {
            switch self {
            case .gpuUtil:
                return .level(row.gpuUtilPct, warn: ClusterSmallGrid.gpuUtilWarn,
                              critical: ClusterSmallGrid.gpuUtilCritical)
            case .gpuTemp: return row.gpuTempLevel
            case .cpuTemp: return row.cpuTempLevel
            case .gpuPower: return row.gpuPowerLevel
            }
        }

        /// The number behind the cell, for a surface that draws a bar rather
        /// than only the text. Only GPU% has a 0...100 scale to draw against.
        func value(of row: CLIMonitorHostRow) -> Double? {
            guard case .gpuUtil = self else { return nil }
            return row.gpuUtilPct
        }
    }

    /// One cell of the grid: the CLI's text, the view-model's verdict, and the
    /// column so a view can label it in its accessibility tree.
    struct Cell: Equatable, Identifiable {
        let column: Column
        let text: String
        let level: ClusterMetricLevel
        /// 0...1 for the GPU column's bar, nil when the value was not told or
        /// the column has no scale.
        let fraction: Double?
        /// The recorder-backed trend for the GPU column, or nil — the view
        /// draws the sparkline where this is set and the single-sample bar
        /// where only `fraction` is. Other columns never carry one.
        let trend: Trend?

        var id: String { column.rawValue }

        init(column: Column, row: CLIMonitorHostRow, trend: Trend? = nil) {
            self.column = column
            text = column.cellText(in: row)
            level = column.level(of: row)
            self.trend = column == .gpuUtil ? trend : nil
            if let raw = column.value(of: row) {
                fraction = min(max(raw / 100, 0), 1)
            } else {
                fraction = nil
            }
        }
    }

    /// One host: the label the app is allowed to show (never the address, §7)
    /// and one cell per `Column`, in `Column.allCases` order.
    struct Row: Equatable, Identifiable {
        let id: String
        let label: String
        let cells: [Cell]
        /// The CLI's error row: this host reported no sample, so every numeric
        /// cell is `-`. It still gets a line — a host that is down is not
        /// absent, and the tile that hides it reports a smaller fleet than the
        /// CLI printed.
        let isDown: Bool

        init(row: CLIMonitorHostRow, trend: Trend? = nil) {
            id = row.host
            label = row.displayLabel
            isDown = row.error != nil
            cells = Column.allCases.map { Cell(column: $0, row: row, trend: trend) }
        }
    }

    /// The serving totals line — the CLI's `total (n)` rows reduced to the two
    /// numbers the operator reads off a small tile, plus how many models that
    /// came from.
    struct ServingTotals: Equatable {
        /// nil = the route sent no serving section at all. 0 = it sent one and
        /// nothing is running; those are different sentences (§6 rule 1).
        let running: Int?
        let waiting: Int?
        let models: Int?
        /// The CLI's own `_load_style`, applied to the summed pair: a queue is
        /// `warn`, a busy engine is `ok`, an idle one `dim`.
        let level: ClusterMetricLevel

        /// The route sent no serving section (or its poll failed): the two
        /// numbers are `-`, and `-` is the only honest rendering of "not told".
        static let notTold = ServingTotals(running: nil, waiting: nil, models: nil,
                                           level: .unknown)

        /// The CLI's `total (n)` rows reduced to the tile's line. A factory
        /// rather than an `init` because the not-told case must return the
        /// shared `notTold` value, and an `init` that assigns `self = .notTold`
        /// while `notTold` is built by that same `init` deadlocks the lazy
        /// static on its second use.
        static func summing(_ totalRows: [CLIMonitorServingTotal],
                            hasServingSection: Bool) -> ServingTotals {
            guard hasServingSection else { return .notTold }
            let running = totalRows.reduce(0) { $0 + $1.running }
            let waiting = totalRows.reduce(0) { $0 + $1.waiting }
            return ServingTotals(running: running, waiting: waiting,
                                 models: totalRows.count,
                                 level: CLIMonitorFormat.loadLevel(running: running,
                                                                   waiting: waiting))
        }
    }

    // MARK: - The GPU% trend (t_2681d3de)

    /// The metric every host sparkline draws. One metric for the whole tile
    /// keeps the tile honest in the cheapest way: one request, one series per
    /// host, and the spoken trend cannot disagree with the digit above it
    /// because they describe the same field.
    static let trendMetric = ClusterHistoryMetric.gpuUtilPct

    /// The window the small family asks the recorder for. `1h` is the
    /// durable 1-minute tier (tier 1) — `15m` would ask for the daemon's
    /// 2 s ring, which the API process cannot read and answers empty with a
    /// `note` (the committed tier-0 fixture is exactly that answer), and a
    /// flat line drawn from "the ring was not resident" would be the lie
    /// this tile exists to avoid.
    static let trendWindow = "1h"

    /// How many pixel columns the cell's track gets. The downsampler emits at
    /// most two points per column (its min and its max), so this bounds the
    /// polyline at ~2x that many vertices — plenty for a track the width of
    /// three digits, and few enough that no vertex lands inside another.
    static let sparkPixelColumns = 13

    /// One vertex of a sparkline, normalised into the track by the model half
    /// so the SwiftUI half multiplies by width and height and compares
    /// nothing. `y` is 0 at the series' own minimum and 1 at its maximum.
    struct TrendPoint: Equatable {
        let x: Double
        let y: Double
    }

    /// The drawable trend of one host's GPU utilisation. Everything numeric
    /// about it comes from the history payload: the extremes are the whole
    /// series' (the reduction is min/max preserving), the vertices are real
    /// samples, and the spoken text formats those extremes the way the CLI
    /// formats the GPU% column. A `nil` trend means the recorder had nothing
    /// to draw — and "nothing to draw" renders as the single-sample bar, not
    /// as a flat line.
    struct Trend: Equatable {
        /// Contiguous runs of the reduced polyline. More than one run means a
        /// gap the recorder actually left (no reading for longer than the
        /// break threshold), and the view must draw each run as its own
        /// stroke rather than connect across it.
        let segments: [[TrendPoint]]
        /// The whole series' min/max, formatted as the CLI formats the GPU%
        /// column. A caption reads these, never the vertices.
        let minText: String
        let maxText: String
        /// The GPU% band of the series' LATEST readable point — the same
        /// verdict style the digit above the track takes, so a line that is
        /// calm where it ends is calm, not the colour of its own past.
        let level: ClusterMetricLevel

        /// The sentence fragment for this cell. Built beside the data it
        /// describes and only ever appended when the sparkline was drawn.
        var accessibilityPart: String {
            "\(Column.gpuUtil.cliColumn) trend \(minText) to \(maxText)"
        }
    }

    /// The host-keyed trends off one history answer. The key is the payload's
    /// own `host` identity — an address, used as a join key and never drawn.
    /// A host the payload excludes, omits or never gives two readable points
    /// for simply has no entry, which is how "not told" stays not told.
    static func trends(from history: ClusterHistoryResponse?) -> [String: Trend] {
        guard let history else { return [:] }
        var out: [String: Trend] = [:]
        for series in ClusterHistorySeriesSet.make(history, for: trendMetric).series {
            if let trend = trend(for: series) { out[series.identity] = trend }
        }
        return out
    }

    /// Reduce one host series to the track. Nil whenever a polyline would be
    /// a fiction: fewer than two readable points is not a trend, and no
    /// segment may be a single point either (a lone vertex is a dot, and a
    /// dot at some remembered height reads as a level the series may never
    /// have held).
    static func trend(for series: ClusterHistorySeries) -> Trend? {
        guard series.readable.count >= 2 else { return nil }
        let reduced = ClusterHistoryDownsampler.downsample(series,
                                                           pixelWidth: sparkPixelColumns)
        guard let low = reduced.outMin, let high = reduced.outMax,
              let firstPoint = reduced.points.first,
              let lastPoint = reduced.points.last,
              let latest = series.readable.last?.value else { return nil }
        let span = lastPoint.t - firstPoint.t
        let range = high - low
        let segments = reduced.segments().compactMap { run -> [TrendPoint]? in
            let points = run.compactMap { sample -> TrendPoint? in
                guard let value = sample.value else { return nil }
                let x = span > 0 ? (sample.t - firstPoint.t) / span : 0
                // A flat series is a level, and a level is a centred line —
                // a layout convention, not a number (both texts print the
                // same value either way).
                let y = range > 0 ? (value - low) / range : 0.5
                return TrendPoint(x: x, y: y)
            }
            return points.count >= 2 ? points : nil
        }
        guard !segments.isEmpty else { return nil }
        return Trend(segments: segments,
                     minText: CLIMonitorFormat.cell(trendMetric.displayValue(low), places: 0),
                     maxText: CLIMonitorFormat.cell(trendMetric.displayValue(high), places: 0),
                     level: .level(latest, warn: gpuUtilWarn, critical: gpuUtilCritical))
    }

    /// Everything one small tile draws, as data. `nil` from `tile(from:)` means
    /// there was nothing to draw: the caller shows its gate pane (the setup
    /// invite, "Paused", or why the read failed) instead of a grid of dashes.
    struct Tile: Equatable {
        /// The CLI's host rows, capped at `maxHosts`, in the route's order.
        let rows: [Row]
        /// Hosts the tile could not fit — printed so the tile never reads as
        /// "this is the whole fleet" when the CLI printed more.
        let hiddenHosts: Int
        /// `host_table`'s count (the CLI title line's number).
        let hostCount: Int
        let totals: ServingTotals

        /// One sentence for VoiceOver, built beside the cells it describes. The
        /// view applies it with `children: .ignore`, so the tile is one element
        /// that speaks the truth instead of twenty fragments a listener has to
        /// reassemble — and the sentence cannot drift from the grid, because it
        /// is composed from the same cells the grid draws (and the same reduced
        /// series a drawn sparkline strokes).
        var accessibilitySummary: String {
            var parts: [String] = []
            if hostCount > 0 {
                parts.append("\(hostCount) host\(hostCount == 1 ? "" : "s")")
            }
            for row in rows {
                var line = row.label + ": " + row.cells.map(\.text).joined(separator: ", ")
                for cell in row.cells {
                    if let trend = cell.trend { line += ", " + trend.accessibilityPart }
                }
                parts.append(line)
            }
            if hiddenHosts > 0 {
                parts.append("\(hiddenHosts) more not shown")
            }
            parts.append("serving \(ClusterSmallGrid.countText(totals.running)) running, "
                         + "\(ClusterSmallGrid.countText(totals.waiting)) waiting")
            return parts.joined(separator: ". ")
        }

        /// Whether any drawn cell carries a recorder-backed trend. The view
        /// needs no such answer (it decides per cell), but the coverage docs
        /// and the gate name the difference between "a tile with trends" and
        /// "a tile of bars", and it must be read off the data, not the layout.
        var hasTrends: Bool {
            rows.contains { row in row.cells.contains { $0.trend != nil } }
        }
    }

    /// Project the view-model onto the tile, joining each host onto its
    /// recorder-backed trend by the payload's own `host` identity (a join key,
    /// never a drawn string). Nil for a nil view-model (the read failed) or a
    /// payload that carried neither a host row nor a serving row: a degraded
    /// `{speak}` body has nothing to grid, and drawing its emptiness as a
    /// table of dashes would read as "four hosts, all unreadable". A nil (or
    /// unusable) `history` is NOT such a case: the grid still draws, and the
    /// GPU cells fall back to their single-sample bars.
    static func tile(from view: ClusterMonitorViewModel?,
                     history: ClusterHistoryResponse? = nil) -> Tile? {
        guard let view, !view.hostRows.isEmpty || !view.servingRows.isEmpty else {
            return nil
        }
        let trends = trends(from: history)
        let shown = view.hostRows.prefix(maxHosts).map {
            Row(row: $0, trend: trends[$0.host])
        }
        return Tile(rows: Array(shown),
                    hiddenHosts: max(0, view.hostRows.count - maxHosts),
                    hostCount: view.hostCount,
                    totals: ServingTotals.summing(view.totalRows,
                                                  hasServingSection: view.hasServingSection))
    }
}
