import Foundation

// ===========================================================================
// ClusterGraphReading.swift — WHAT each graph-widget size draws, and the
// numbers that sit beside it. Foundation only: no SwiftUI, no WidgetKit, no
// Charts, no `Color`.
//
// That restriction is what lets `scripts/cluster_graph_check.sh` compile and
// RUN this file on macOS against the committed `v1_cluster_history_*.json`
// fixtures (the same shape `Sources/Shared/ClusterNodeTally.swift` proves works
// for both platforms and both harness sets). It is also the split the sibling
// macOS card asked for: the desktop embeds these rules and the shared marks by
// path, so a projection that imported WidgetKit could be compiled there but
// never asserted.
//
// THREE LAYERS THIS FILE STANDS ON AND DOES NOT RE-DECLARE.
//
//   * `ClusterHistorySeries.swift` (t_b6512c2e) owns the metric vocabulary
//     (`ClusterHistoryMetric`, whose raw values are the recorder's own column
//     names), the per-metric projection (`ClusterHistorySeriesSet.make`) and the
//     min/max-preserving pixel reduction (`ClusterHistoryDownsampler`). This
//     file calls all three and declares no metric, no bucketing and no stride.
//   * `ClusterMonitorViewModel.swift` (t_61ed3f5e, PR #404) owns the threshold
//     verdicts (`ClusterMetricLevel`) and the numeric cell formatting
//     (`CLIMonitorFormat`). This file applies them to a SERIES instead of to the
//     last sample and never re-decides a band: the two pairs it repeats
//     (`ClusterGraphBand.thermal`, `.load`) are pinned to the view-model by
//     executed band-edge cases in the gate, so a drift reddens a check rather
//     than quietly changing a colour.
//   * `GET /v1/cluster/history` is the ONLY history read (architect ruling
//     DECISION-t_447cc7fc: it is canonical, `/v1/metrics/history` is retired),
//     reached only through `WidgetDataClient.clusterHistory`. Nothing here names
//     a route, and nothing here reads `MetricsHistoryTrend`.
//
// WHAT THIS FILE ADDS, and why it is not a second opinion on the layers above:
//
//   1. The DENSITY AXIS. The card demands four different widgets in one:
//      small = one metric sparkline with current/min/max; medium = one node's
//      temps plus GPU% and their current values; large/extra-large = small
//      multiples, one panel per node with the card's eight metrics, axis labels
//      and a legend. Which metrics a size draws is a RULE, and a rule in a view
//      is a rule no gate can run, so it lives here (`metricsToDraw`).
//   2. The PANEL. A large widget groups several metrics under one node, and two
//      of the eight metrics (`kv`, `gen_tps`) live in `engines[]`, keyed
//      `node:port`, not in `hosts[]`. Attributing an engine series to its node's
//      panel is this file's job and nobody else's (`panels`).
//   3. THE RESOLUTION SENTENCE. The route answers the same `hosts` array at
//      2 s, 1 min and 5 min, and the operator's ruling is that a chart must
//      STATE which one it got, so a 5-minute-spaced 7-day line can never read as
//      live 2-second data (`ClusterGraphResolutionCaption`).
//
// A `nil` stays a `nil` all the way through: the recorder stores nulls for a
// field a node never reported, and a 0 would draw an absent node as an idle one.
// ===========================================================================

// MARK: - Window (the operator's pick)

/// The window the operator selects in the configuration sheet, and the value the
/// route's `window=` parameter takes.
///
/// `recommended` is the operator's ruling on this card, not a default anyone
/// inferred: the Monitor Live Activity and the macOS menu-bar graphs refresh
/// faster than a Home Screen widget, which is exactly where a 2-second graph
/// earns its keep, so a widget dropped on the Home Screen with nothing
/// configured shows 15 minutes. `1h/6h/24h/7d` stay selectable.
enum ClusterGraphWindow: String, CaseIterable, Codable, Equatable {
    case fifteenMinutes = "15m"
    case oneHour = "1h"
    case sixHours = "6h"
    case twentyFourHours = "24h"
    case sevenDays = "7d"

    static let recommended: ClusterGraphWindow = .fifteenMinutes

    /// The `window=` value (`hscc-api/routes_cluster.py`, `_parse_history_window`).
    var requestValue: String { rawValue }

    /// The picker row and the caption spelling.
    var label: String {
        switch self {
        case .fifteenMinutes: return "15 m"
        case .oneHour: return "1 h"
        case .sixHours: return "6 h"
        case .twentyFourHours: return "24 h"
        case .sevenDays: return "7 d"
        }
    }

    /// The tier this window reads, per the route's own mapping (`15m` the 2 s
    /// ring, `1h`/`6h`/`24h` the 1-minute roll-ups, `7d` the 5-minute ones).
    ///
    /// Only the REQUEST needs a guess here — the server states the tier it
    /// served on the payload (`response.tier`) and everything downstream reads
    /// THAT. This exists because the peak columns cannot be requested at tier 0
    /// (the route answers 400), so the request list has to know before it asks.
    var expectedTier: Int {
        switch self {
        case .fifteenMinutes: return 0
        case .oneHour, .sixHours, .twentyFourHours: return 1
        case .sevenDays: return 2
        }
    }
}

// MARK: - The resolution sentence

/// The served resolution, phrased so a chart can never be mistaken for a faster
/// one than it is.
///
/// The payload carries `resolution` as the route spells it (`"2s"`, `"60s"`,
/// `"300s"`) and `resolution_s` as the number. The caption is built from the
/// NUMBER, because the operator's ruling asked for the human form ("2 s" vs
/// "5 m") and `60s`/`300s` are the same fact written worse. Nothing is
/// invented: `resolution_s` is the server's own statement of its own step.
enum ClusterGraphResolutionCaption {
    /// `2` → "2 s", `60` → "1 m", `300` → "5 m". A non-multiple of 60 keeps one
    /// decimal rather than rounding a resolution into a lie.
    static func text(resolutionS: Int) -> String {
        guard resolutionS > 0 else { return "unknown" }
        if resolutionS < 60 { return "\(resolutionS) s" }
        if resolutionS % 60 == 0 { return "\(resolutionS / 60) m" }
        return String(format: "%.1f m", Double(resolutionS) / 60)
    }

    /// The line a surface puts under the chart. "every 5 m" states the step, so
    /// a sparse line reads as sparse; "5 m" alone could be read as a range.
    static func line(resolutionS: Int) -> String { "every \(text(resolutionS: resolutionS))" }

    /// The full honesty line: the window the operator asked for AND the step the
    /// server actually served. They differ by design (a 7 d window is 2016
    /// five-minute buckets) and a caption that shows only one of them lets the
    /// reader supply the other.
    static func line(window: ClusterGraphWindow, resolutionS: Int) -> String {
        "\(window.label) · every \(text(resolutionS: resolutionS))"
    }
}

// MARK: - Density axis

/// The four Home Screen sizes folded onto the three layouts the card specifies.
///
/// Deliberately NOT a `WidgetFamily`: `Sources/Shared/` must stay compilable by
/// a macOS `swiftc` harness, and `WidgetFamily` is an Int-backed WidgetKit type
/// whose accessory cases are `@available(macOS, unavailable)`. The family → density
/// map is therefore a positive list in `Sources/HSCCWidgets/GraphWidgetMetrics.swift`,
/// which lets the harness assert the true side and leaves the false side to a
/// structural grep.
enum ClusterGraphDensity: String, CaseIterable, Codable, Equatable {
    /// `systemSmall`: ONE metric, one line, current + min + max.
    case sparkline
    /// `systemMedium`: one node's temperatures plus GPU utilisation, each with
    /// its current value.
    case nodeOverlay
    /// `systemLarge` / `systemExtraLarge`: small multiples, one panel per node.
    case smallMultiples
}

// MARK: - Metric selection per size

extension ClusterHistoryMetricGroup {
    /// The picker's row name. The set vocabulary stays the shared layer's; this
    /// only spells it out for a human, which is why it is an extension here
    /// rather than a second enum.
    var graphLabel: String {
        switch self {
        case .temps: return "Temperatures"
        case .utilisation: return "Utilisation"
        case .power: return "Power"
        case .memory: return "Memory"
        case .serving: return "Serving"
        }
    }

    /// The one metric a `sparkline` draws for this set.
    ///
    /// The GPU-side number leads wherever one exists, because the fleet is a GPU
    /// fleet and the CPU temperature is the one a hot GPU drags up with it
    /// rather than a separate fact. `serving` has no host-side number at all, so
    /// its lead is the engine's own output rate.
    var graphLeadMetric: ClusterHistoryMetric {
        switch self {
        case .temps: return .gpuTempC
        case .utilisation: return .gpuUtilPct
        case .power: return .gpuPowerW
        case .memory: return .memUsedPct
        case .serving: return .genTps
        }
    }
}

/// What a size draws, which metrics to ask the route for, and what each panel
/// contains. Every rule here is a pure function of (size, metric set, tier).
enum ClusterGraphRule {
    /// The eight metrics the card names for a large / extra-large panel, in the
    /// card's own order: CPU°C, GPU°C, GPU%, CPU%, W, RAM%, KV%, tok/s.
    ///
    /// The card's "RAM%" is `mem_used_pct`, and its "tok/s" is `gen_tps` — the
    /// generation side, which is the rate a reader of a serving panel wants.
    /// `prompt_tps` exists in the same set and is deliberately NOT in this list:
    /// the card's panel is one row per node and the prompt rate belongs to an
    /// engine's input side, which the panel's legend would have to explain.
    /// KV% and tok/s are ENGINE series (`seriesKind == .engine`), keyed
    /// `node:port` in `engines[]`, which is why a panel has to attribute them
    /// rather than assume every metric arrives keyed by host.
    static let cardPanelMetrics: [ClusterHistoryMetric] = [
        .cpuTempC, .gpuTempC, .gpuUtilPct, .cpuUtilPct,
        .gpuPowerW, .memUsedPct, .kvUsage, .genTps
    ]

    /// The medium layout's list when the operator left the metric set unset:
    /// "all temps plus GPU%", i.e. the two temperatures and the GPU utilisation.
    static let cardOverlayMetrics: [ClusterHistoryMetric] = [.cpuTempC, .gpuTempC, .gpuUtilPct]

    /// The metrics a size draws, filtered to what the served tier can answer.
    ///
    /// `set == nil` means "All metric sets" (the picker's sentinel, folded to nil
    /// by the intent so no view downstream knows the sentinel exists). The tier
    /// filter is not decoration: the three `*_max` columns exist only after a
    /// roll-up and the route answers 400 for them at tier 0, so a peak picked on
    /// the default 15-minute window would fail the REQUEST, not just the chart.
    static func metricsToDraw(density: ClusterGraphDensity,
                              set: ClusterHistoryMetricGroup?,
                              tier: Int) -> [ClusterHistoryMetric] {
        requestable(baseMetrics(density: density, set: set), tier: tier)
    }

    /// The `metrics=` list for one request. One request carries every metric a
    /// size draws — the route takes a comma list and returns one row per
    /// (identity, timestamp) with all requested columns — so the widget stays
    /// inside the contract's request budget at every size.
    static func requestMetrics(density: ClusterGraphDensity,
                               set: ClusterHistoryMetricGroup?,
                               window: ClusterGraphWindow) -> [ClusterHistoryMetric] {
        requestable(baseMetrics(density: density, set: set), tier: window.expectedTier)
    }

    /// The metrics before the tier filter, in the order they should be drawn.
    static func baseMetrics(density: ClusterGraphDensity,
                            set: ClusterHistoryMetricGroup?) -> [ClusterHistoryMetric] {
        switch density {
        case .sparkline:
            return [(set ?? .temps).graphLeadMetric]
        case .nodeOverlay:
            guard let set else { return cardOverlayMetrics }
            return ClusterHistoryMetric.allCases.filter { $0.group == set }
        case .smallMultiples:
            guard let set else { return cardPanelMetrics }
            let inSet = ClusterHistoryMetric.allCases.filter { $0.group == set }
            // Keep the CARD's order for the metrics that appear in both lists, so
            // an operator who narrows the set sees the same columns in the same
            // places, only fewer of them; then any set member the card's panel
            // list never carried (VRAM MB, prompt t/s, the peaks).
            return cardPanelMetrics.filter { $0.group == set }
                + inSet.filter { !cardPanelMetrics.contains($0) }
        }
    }

    /// Drop what this tier cannot answer, keeping order.
    static func requestable(_ metrics: [ClusterHistoryMetric],
                            tier: Int) -> [ClusterHistoryMetric] {
        metrics.filter { $0.requestable(atTier: tier) }
    }

    /// The metrics a request would ask for that the served tier then refused to
    /// fill — the honest explanation for a panel whose peaks came back empty
    /// because the payload's tier is not the window's expected tier.
    static func droppedAtTier(_ metrics: [ClusterHistoryMetric],
                              tier: Int) -> [ClusterHistoryMetric] {
        metrics.filter { !$0.requestable(atTier: tier) }
    }
}

// MARK: - Threshold bands

/// One painted band of a value range, for the temperature charts the card asks
/// for ("threshold bands for temperatures").
struct ClusterGraphBand: Equatable {
    let from: Double
    let to: Double
    let level: ClusterMetricLevel
}

/// The two verdict pairs this file applies, and the only numbers in it that look
/// like thresholds.
///
/// BOTH pairs are the view-model's (`CLIMonitorHostRow.cpuTempLevel`,
/// `gpuTempLevel`, `memLevel`, `cpuLevel`), restated because a chart has to
/// apply a band to EVERY point of a series while the view-model applies it to
/// the last sample. That makes this a copy, so the gate keeps the copy honest:
/// `cluster_graph_check.sh` builds a real `CLIMonitorHostRow` from synthetic
/// `MonitorNode`s at the four band edges of each pair and fails if the
/// view-model's verdict and `ClusterGraphBand.level(of:)` ever disagree. It is a
/// copy with a tripwire, not a second opinion.
enum ClusterGraphBandRule {
    /// °C, from `CLIMonitorHostRow`'s thermal columns.
    static let thermalWarnC = 80.0
    static let thermalCriticalC = 90.0
    /// Percent-used, from `CLIMonitorHostRow.memLevel` / `cpuLevel`.
    static let loadWarnPct = 85.0
    static let loadCriticalPct = 95.0

    /// The thermal verdict for one sample of a temperature series.
    static func thermal(_ celsius: Double?) -> ClusterMetricLevel {
        ClusterMetricLevel.level(celsius, warn: thermalWarnC, critical: thermalCriticalC)
    }

    /// The load verdict for one sample of a percent-used series.
    static func load(_ percent: Double?) -> ClusterMetricLevel {
        ClusterMetricLevel.level(percent, warn: loadWarnPct, critical: loadCriticalPct)
    }

    /// The verdict a metric's line should carry, or nil when NO declared rule
    /// exists for it.
    ///
    /// `nil` is the honest answer for more metrics than one would expect, and it
    /// is why the line type distinguishes "verdict" from "unthresholded" instead
    /// of painting everything: the view-model's own header says the monitor CLI
    /// thresholds exactly ONE thing and every other band is its own declared
    /// rule. GPU utilisation, wattage, KV occupancy and token rates have no
    /// declared band, so a colour for them would be this file inventing one.
    /// (Wattage is the clearest case: the view-model's power rule divides by
    /// `gpu_power_limit_w`, and there is no limit column in the recorder, so the
    /// denominator the verdict needs was never stored.)
    static func verdict(for metric: ClusterHistoryMetric,
                        displayValue: Double?) -> ClusterMetricLevel? {
        switch metric {
        case .cpuTempC, .cpuTempCMax, .gpuTempC, .gpuTempCMax:
            return thermal(displayValue)
        case .memUsedPct, .cpuUtilPct:
            return load(displayValue)
        default:
            return nil
        }
    }

    /// The bands to paint behind a temperature chart, clipped to `[floor, top]`.
    ///
    /// Only temperatures get bands (`verdict(for:)` above says why). `top` is the
    /// chart's own y-max so a band never extends past the data the reader is
    /// comparing against; a band whose upper edge is above the axis is invisible
    /// and an invisible band is decoration, not information.
    static func temperatureBands(from floor: Double, top: Double) -> [ClusterGraphBand] {
        let edges: [(Double, Double, ClusterMetricLevel)] = [
            (thermalWarnC, thermalCriticalC, .warn),
            (thermalCriticalC, .greatestFiniteMagnitude, .critical)
        ]
        var out: [ClusterGraphBand] = []
        for (low, high, level) in edges {
            guard top > low else { continue }
            out.append(ClusterGraphBand(from: max(floor, low),
                                        to: min(top, high),
                                        level: level))
        }
        return out
    }

    /// The y floor a temperature chart should start at. Chart axis from 0 makes a
    /// 60→88 °C excursion read as a flat line, which is the one thing a thermal
    /// chart exists to prevent; the band edges are what anchor the reader.
    static func temperatureAxisFloor(maximum: Double?) -> Double {
        guard let maximum else { return 0 }
        return max(0, (minimumDisplayedCeiling(for: maximum) / 10).rounded(.down) * 10 - 10)
    }

    /// The band edge a value reaches, or the value itself rounded up when it is
    /// below every band — so an all-quiet fleet still gets an axis that shows
    /// the quiet rather than a 0-1 axis.
    private static func minimumDisplayedCeiling(for value: Double) -> Double {
        if value >= thermalCriticalC { return value }
        if value >= thermalWarnC { return thermalCriticalC }
        return thermalWarnC
    }
}

// MARK: - Line tint

/// How a drawn line is coloured — and the difference between the two cases is
/// the whole point.
enum ClusterGraphTint: Equatable {
    /// A declared band exists for this metric and this is its verdict on the
    /// line's current value.
    case verdict(ClusterMetricLevel)
    /// No declared band exists. The view draws the line in the style's accent
    /// (a fact about "this is a line"), never in a status colour (a claim about
    /// "this value is fine"), because an invented `.ok` green is exactly the
    /// verdict this file refuses to make up.
    case unthresholded
}

// MARK: - The drawn line

/// One line on one chart: one metric off one identity, reduced to the width the
/// view measured, in the unit the view will print.
struct ClusterGraphLine: Equatable {
    let metric: ClusterHistoryMetric
    /// The recorder's own row identity (`host`, or `node:port` for an engine).
    let identity: String
    /// The §7-safe label for the legend: never an address.
    let label: String
    /// The reduction's own points, oldest first. Never a stride-thin: every
    /// pixel column's own minimum AND maximum is in here.
    let points: [ClusterHistorySample]
    /// `points` split where a retained step is wider than the reduction's break,
    /// so a window with no reading is a GAP and not a slope between the two
    /// nearest numbers.
    let segments: [[ClusterHistorySample]]
    /// The right-hand end — the "now" a widget headlines.
    let current: Double?
    /// The whole series' extremes BEFORE thinning. The parent's rule: a caption
    /// that reads the thinned array is a claim about which points survived.
    let minimum: Double?
    let minimumAt: TimeInterval?
    let maximum: Double?
    let maximumAt: TimeInterval?
    let tint: ClusterGraphTint
    let downsampled: ClusterHistoryDownsampled
    /// How many points the recorder sent for this line.
    let sourceCount: Int
    /// Points that carried no value — the recorder's own nulls.
    let missingCount: Int

    /// The property the reduction exists to have, exposed so a surface (and the
    /// gate) can assert it instead of trusting it.
    var extremesPreserved: Bool { downsampled.extremesPreserved }
    var wasThinned: Bool { downsampled.thinned }
    var gapColumns: Int { downsampled.gapColumns }
    var hasValue: Bool { current != nil || minimum != nil }
}

// MARK: - The panel

/// One panel of the small-multiples layout: ONE node, the lines the size asked
/// for, and the caption facts.
struct ClusterGraphPanel: Equatable {
    /// The recorder's host value. Held because engine rows must be attributed
    /// back to it; NEVER rendered — `displayLabel` is the renderable one.
    let host: String
    /// The §7-safe header: the shared `.NNN` tail rule, or the engine label for an
    /// engine-only panel.
    let displayLabel: String
    let lines: [ClusterGraphLine]

    var hasAnyValue: Bool { lines.contains { $0.hasValue } }
    var readableLineCount: Int { lines.filter { $0.hasValue }.count }
}

// MARK: - Building lines and panels

/// Turns one `GET /v1/cluster/history` answer into the lines and panels a size
/// draws. Every entry point takes the PIXEL WIDTH the view measured, because a
/// number that decides how many points survive must be the same number that
/// decides how many axis labels fit — see `ClusterGraphAxisRule`.
enum ClusterGraphReading {
    /// The all-nodes identity, used when the operator left the node picker
    /// unset. Not a host name, so it can never collide with one, and the label
    /// states the host count so "all" is never a bare word.
    static let allNodesIdentity = "*"

    /// One line: one metric off one identity, reduced to `pixelWidth`.
    ///
    /// The display transform (KV's 0-1 fraction becomes a percentage via the
    /// shared `displayFactor`) is applied BEFORE the reduction, so min, max and
    /// every drawn point are in the unit the caption prints. Applying it
    /// afterwards would be fine for min/max and wrong for nothing, but applying
    /// it before means the reduction can never be accused of bucketing in one
    /// unit and labelling in another.
    static func line(metric: ClusterHistoryMetric,
                     series: ClusterHistorySeries,
                     pixelWidth: Int) -> ClusterGraphLine {
        let display = series.samples.map {
            ClusterHistorySample(t: $0.t, value: metric.displayValue($0.value))
        }
        let reduced = ClusterHistoryDownsampler.downsample(display,
                                                           resolutionS: series.resolutionS,
                                                           pixelWidth: pixelWidth)
        return makeLine(metric: metric,
                        identity: series.identity,
                        label: displayLabel(for: metric.seriesKind, identity: series.identity),
                        display: display,
                        reduced: reduced)
    }

    /// The fleet envelope: ONE line whose per-column min/max is the whole
    /// fleet's. Used by `sparkline` and `nodeOverlay` when the node picker is
    /// "All", because "all nodes" for a single line is precisely a min/max
    /// envelope — and because the parent layer is the only place that folds
    /// several series together, and it REFUSES a mixed tier rather than
    /// averaging a 2 s sample into a 5-minute average.
    ///
    /// Returns nil when there is nothing to fold or the series disagree about
    /// tier or resolution. A nil here is rendered as "nothing to draw", never as
    /// a flat zero line.
    static func envelope(metric: ClusterHistoryMetric,
                         series: [ClusterHistorySeries],
                         pixelWidth: Int) -> ClusterGraphLine? {
        guard !series.isEmpty else { return nil }
        let display = series.map { owner in
            ClusterHistorySeries(metric: owner.metric,
                                 identity: owner.identity,
                                 tier: owner.tier,
                                 resolutionS: owner.resolutionS,
                                 samples: owner.samples.map {
                                    ClusterHistorySample(t: $0.t,
                                                         value: metric.displayValue($0.value))
                                 })
        }
        guard let reduced = ClusterHistoryDownsampler.downsample(display,
                                                                pixelWidth: pixelWidth) else {
            return nil
        }
        let merged = display.flatMap { $0.samples }
        let hostCount = Set(series.filter { $0.metric.seriesKind == .host }.map { $0.identity }).count
        let subject = hostCount > 0 ? "\(hostCount) node(s)" : "\(series.count) engine(s)"
        return makeLine(metric: metric,
                        identity: allNodesIdentity,
                        label: "all nodes (\(subject))",
                        display: merged,
                        reduced: reduced)
    }

    /// The panels of a small-multiples chart, one per node, in sorted identity
    /// order so two renders of the same payload draw the same layout.
    ///
    /// Engine metrics are attributed to the node that runs them: an engine row's
    /// identity is `node:port` (the recorder separates engines by port because
    /// one host can run two backends on two GPUs), and a panel draws the engine
    /// lines whose host part equals the panel's host. A panel with no engine rows
    /// simply has no engine lines — it does not draw a zero.
    static func panels(response: ClusterHistoryResponse,
                       metrics: [ClusterHistoryMetric],
                       pixelWidth: Int,
                       limit: Int? = nil) -> [ClusterGraphPanel] {
        guard !metrics.isEmpty else { return [] }
        let hosts = hostIdentities(response: response, metrics: metrics)
        var panels: [ClusterGraphPanel] = []
        for host in hosts {
            var lines: [ClusterGraphLine] = []
            for metric in metrics {
                let set = ClusterHistorySeriesSet.make(response, for: metric)
                switch metric.seriesKind {
                case .host:
                    for series in set.series where series.identity == host {
                        lines.append(line(metric: metric, series: series,
                                         pixelWidth: pixelWidth))
                    }
                case .engine:
                    for series in set.series where engineHost(series.identity) == host {
                        lines.append(line(metric: metric, series: series,
                                         pixelWidth: pixelWidth))
                    }
                }
            }
            panels.append(ClusterGraphPanel(host: host,
                                            displayLabel: MonitorNode.shortTail(host),
                                            lines: lines))
        }
        if let limit, limit > 0, panels.count > limit {
            return Array(panels.prefix(limit))
        }
        return panels
    }

    /// The nodes a small-multiples chart has a row for: every host that appears
    /// in a host-series the chart draws, so a panel never exists with no metric
    /// of its own. Sorted by code point so the layout is stable.
    static func hostIdentities(response: ClusterHistoryResponse,
                               metrics: [ClusterHistoryMetric]) -> [String] {
        var seen = Set<String>()
        for metric in metrics where metric.seriesKind == .host {
            for set in [ClusterHistorySeriesSet.make(response, for: metric)] {
                for identity in set.identities where identity != "?" {
                    seen.insert(identity)
                }
            }
        }
        // A chart whose only metrics are engine ones still needs panels: take the
        // engine rows' own host part, and when even that is absent, fall back to
        // the payload's host list so the widget can say "excluded" with a name.
        if seen.isEmpty {
            for metric in metrics where metric.seriesKind == .engine {
                for set in [ClusterHistorySeriesSet.make(response, for: metric)] {
                    for identity in set.identities {
                        if let host = engineHost(identity) { seen.insert(host) }
                    }
                }
            }
        }
        if seen.isEmpty {
            for row in response.hosts { if let host = row.host { seen.insert(host) } }
        }
        return seen.sorted { $0.utf8.lexicographicallyPrecedes($1.utf8) }
    }

    /// The host part of an engine identity (`node:port`), or nil when the row had
    /// no node at all.
    static func engineHost(_ identity: String) -> String? {
        guard let split = identity.firstIndex(of: ":") else { return nil }
        let host = identity[..<split]
        return host.isEmpty ? nil : String(host)
    }

    /// The §7-safe label for one identity.
    ///
    /// `MonitorNode.shortTail` is THE app rule for turning a host value into a
    /// label (`.NNN` tail, or the value itself when it is not a dotted quad) and
    /// this calls it rather than copying it, so a widget, a Live Activity, a live
    /// probe and this chart cannot disagree about a node's name. The history
    /// payload carries no hostname column, so the tail is all there is to show —
    /// and an address is never what a Home Screen should show.
    static func displayLabel(for kind: ClusterHistorySeriesKind, identity: String) -> String {
        switch kind {
        case .host:
            return MonitorNode.shortTail(identity)
        case .engine:
            guard let split = identity.firstIndex(of: ":") else {
                return MonitorNode.shortTail(identity)
            }
            let host = String(identity[..<split])
            let port = String(identity[identity.index(after: split)...])
            if port.isEmpty { return MonitorNode.shortTail(host) }
            let tail = MonitorNode.shortTail(host)
            return tail + ":" + port
        }
    }

    // The shared body of `line` and `envelope`: the display-space samples are
    // already in hand, so current/min/max come from the same arrays the
    // reduction bucketed.
    private static func makeLine(metric: ClusterHistoryMetric,
                                 identity: String,
                                 label: String,
                                 display: [ClusterHistorySample],
                                 reduced: ClusterHistoryDownsampled) -> ClusterGraphLine {
        let current = display.last { $0.value != nil }?.value
        return ClusterGraphLine(
            metric: metric,
            identity: identity,
            label: label,
            points: reduced.points,
            segments: reduced.segments(),
            current: current,
            minimum: reduced.trueMin,
            minimumAt: reduced.trueMinAt,
            maximum: reduced.trueMax,
            maximumAt: reduced.trueMaxAt,
            tint: ClusterGraphBandRule.verdict(for: metric, displayValue: current)
                .map { ClusterGraphTint.verdict($0) } ?? .unthresholded,
            downsampled: reduced,
            sourceCount: display.count,
            missingCount: display.count - display.filter { $0.value != nil }.count)
    }
}

// MARK: - Axis density

/// How many axis labels a width can carry.
///
/// The SAME integer that drives `ClusterGraphDownsampler.downsample(_:pixelWidth:)`
/// drives this, and that is the rule rather than a style note: if the labels come
/// from a different assumption than the buckets, the axis states a time the
/// polyline does not have.
enum ClusterGraphAxisRule {
    /// Points per label: wide enough that the time text does not collide with
    /// its neighbour at any size the app ships.
    static let pointsPerLabel = 60

    static func labelCount(pixelWidth: Int) -> Int {
        max(2, min(6, pixelWidth / pointsPerLabel))
    }

    /// Whole pixels from a measured width. A width of 0 (a widget still laying
    /// out) gets ONE column rather than zero, because a zero-column reduction
    /// would drop every point of a real series.
    static func pixelColumns(measuredWidth: Double) -> Int {
        guard measuredWidth.isFinite, measuredWidth > 0 else { return 1 }
        return max(1, Int(measuredWidth.rounded()))
    }
}

// MARK: - Captions

/// The number lines beside a chart: "now", "min", "max", and the reason a chart
/// is empty when it is.
enum ClusterGraphCaption {
    /// One value with the metric's own unit, and the view-model's dash when the
    /// node never reported it. Never "0" for a nil.
    static func value(_ value: Double?, metric: ClusterHistoryMetric) -> String {
        guard let value else { return CLIMonitorFormat.dash }
        return String(format: "%.\(places(for: metric))f", value) + metric.unit
    }

    /// A value plus the timestamp it happened at, for the min/max a sparkline
    /// must show. `at` comes from `ClusterHistoryExtremes`, i.e. from the real
    /// sample time, not from a bucket edge.
    static func extreme(_ number: Double?,
                        at time: TimeInterval?,
                        metric: ClusterHistoryMetric,
                        clock: (TimeInterval) -> String) -> String {
        let text = value(number, metric: metric)
        guard let time else { return text }
        return "\(text) @ \(clock(time))"
    }

    /// Per-metric decimal places, taken from the CLI's own column precisions in
    /// `cluster_live.py` so a chart and `hscc cluster monitor` cannot print two
    /// different numbers for one reading: host table GPU% 0, CPU% 1, RAM% 1,
    /// CPU temp 1, GPU temp 0, GPU power 2; serving table KV% 1, Gen t/s 1,
    /// Prompt t/s 0. A column the CLI has no cell for (VRAM MB, run, wait) is a
    /// count or a byte figure and takes 0.
    static func places(for metric: ClusterHistoryMetric) -> Int {
        switch metric {
        case .cpuTempC, .cpuTempCMax: return 1
        case .gpuTempC, .gpuTempCMax: return 0
        case .gpuUtilPct: return 0
        case .cpuUtilPct, .memUsedPct: return 1
        case .gpuPowerW, .gpuPowerWMax: return 2
        case .vramUsedMb: return 0
        case .kvUsage: return 1
        case .genTps: return 1
        case .promptTps: return 0
        case .runRequests, .waitRequests: return 0
        }
    }

    /// Why a chart has nothing in it, in the payload's own words.
    ///
    /// The shared layer already separates "the `metrics=` filter named no field
    /// of this series" from "the fleet had nothing to serve"; a chart that
    /// rendered the second as a flat quiet fleet would be the §6 lie, so the
    /// reason is drawn from `ClusterHistorySeriesSet`, not re-guessed here.
    static func emptyReason(for set: ClusterHistorySeriesSet,
                            window: ClusterGraphWindow) -> String {
        let kind = set.metric.seriesKind.rawValue
        if set.isEmptyBecauseExcluded {
            return "the answer excluded the " + kind + " rows for this metric"
        }
        if let dropped = ClusterGraphRule.droppedAtTier([set.metric], tier: set.tier).first,
           let needs = dropped.tierRequirement {
            return dropped.rawValue + " — " + needs
        }
        if let reason = set.reason, !reason.isEmpty { return reason }
        if set.tier == 0 {
            return "no " + window.label + " samples in the recorder's 2 s ring"
        }
        return "no stored samples for " + window.label
    }
}
