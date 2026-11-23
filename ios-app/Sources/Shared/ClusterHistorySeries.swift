import Foundation

// ===========================================================================
// ClusterHistorySeries.swift — the widget-side half of GET /v1/cluster/history.
//
// The route (hscc-api/routes_cluster.py `handle_cluster_history`) answers about
// the PAST, and it answers at three different resolutions: 2 s raw samples
// (tier 0), 1-minute averages (tier 1) and 5-minute averages (tier 2). One
// `hosts` array can therefore mean three completely different charts, and the
// payload states which one it is (`tier`, `resolution_s`, `max_fields`,
// `hosts_included`/`engines_included`, `note`). This file keeps the two rules
// that follow from that shape, in ONE place, so no widget invents either:
//
//   1. NEVER LIE ABOUT MIN/MAX. A chart is drawn into a few hundred pixels, so
//      a long series must be reduced. Reducing it by taking every Nth point
//      (a stride/thin) drops whichever extreme happened to sit in the skipped
//      points, and the widget then prints a peak the data never had — the
//      failure this file exists to prevent. The reduction here is per PIXEL
//      COLUMN: each column emits its own minimum AND maximum, so the thinned
//      polyline carries every column's extremes and therefore the whole
//      series' extremes. The true min/max are also returned alongside the
//      points (with their timestamps), so a caption like "peaked at 88 C" reads
//      them and never the thinned array.
//
//   2. NEVER MIX TIERS IN ONE SERIES. A 2 s raw sample and a 5-minute average
//      are not comparable numbers, and folding both into one pixel column is
//      precisely the min/max lie above. A `ClusterHistorySeries` is built from
//      ONE response (one tier, one `resolution_s`) and the only combining
//      helper REFUSES a mixed set rather than averaging across tiers.
//
//   3. AN AVERAGE IS NEVER DRAWN ALONE WHERE THE TIER SERVED A PEAK, AND A
//      MISSING READING STAYS MISSING. `gpu_temp_c` is a bucket MEAN and
//      `gpu_temp_c_max` is that bucket's PEAK: a chart that draws only the mean
//      of a GPU that touched 91 C for one bucket looks calm, which is the
//      excursion an operator opens the widget to see. `max_fields` is what says
//      whether the peak was served (`[]` at tier 0, where a raw 2 s sample has
//      never been rolled up), and `historyPair(for:)` below is the only way a
//      surface is meant to ask. A metric the payload reported as `null` stays
//      `nil` in `ClusterHistorySample.value` — never a `0.0`, because "idle at
//      zero" and "this node never answered" are different facts and a gap must
//      stay countable (`missingCount`, `gapColumns`, `segments()`).
//
//      Rules 3's wording is not native to this file: it came from
//      `MetricsHistoryTrend.swift`, the projection over the duplicate
//      `GET /v1/metrics/history`, when t_2c629557 retired that route behind
//      this one (the ruling is t_447cc7fc). The endpoint went; the contract it
//      encoded did not.
//
// Everything here is derived from the payload's own fields; no number is
// invented and no field is re-derived. A `nil` value is the recorder's own
// "this node never reported it" and stays nil — never 0, never interpolated —
// because a 0 would draw a node that never answered as an idle one. The
// metric raw values are the recorder's own column names
// (hscc_daemon/metrics_history.py: `HOST_KEYS`, the `*_max` peaks of `MAX_KEYS`,
// and `ENGINE_KEYS`); `scripts/cluster_history_metric_vocabulary.py` fails the
// gate if any of them stops being a real stored column, so a rename in the
// recorder lands here as a red gate rather than as a silently empty chart.
// ===========================================================================

// MARK: - Metric vocabulary

/// Which block of the card's metric list a field belongs to (temps, utilisation,
/// power, memory, serving). A picker groups by this; nothing derives a group
/// from a name at the call site.
enum ClusterHistoryMetricGroup: String, CaseIterable {
    case temps
    case utilisation
    case power
    case memory
    case serving
}

/// Which row array of the payload carries the field. `hosts[]` and `engines[]`
/// are different series with different identity keys, so a chart cannot treat
/// one as a continuation of the other.
enum ClusterHistorySeriesKind: String, CaseIterable {
    case host
    case engine
}

/// Every metric the graph widget may ask the route for.
///
/// The raw value IS the wire key — copied from the recorder's own column
/// tuples, not invented. `metrics=` on the request takes exactly these
/// strings, and the route rejects any other name with a 400 that lists the
/// valid ones, so a key that drifts fails at request time as well as here.
///
/// Two deliberate absences, both measurements rather than choices:
///   * there is no VRAM PERCENT. The recorder stores `gpu_mem_used_mb` but no
///     `gpu_mem_total_mb`, so `vramUsedMb` is a megabyte series and a
///     percentage would need a denominator the route never sends.
///   * there is no peak at `window=15m`: the three `*_max` columns exist only
///     after a roll-up, and the route answers 400 for them at tier 0
///     (`_parse_history_metrics`), which is what `requestable(atTier:)` mirrors.
enum ClusterHistoryMetric: String, CaseIterable, Equatable {
    // temps — averages, plus the peaks the recorder keeps for them
    case cpuTempC = "cpu_temp_c"
    case cpuTempCMax = "cpu_temp_c_max"
    case gpuTempC = "gpu_temp_c"
    case gpuTempCMax = "gpu_temp_c_max"
    // utilisation
    case gpuUtilPct = "gpu_util_pct"
    case cpuUtilPct = "cpu_usage_pct"
    // power
    case gpuPowerW = "gpu_power_w"
    case gpuPowerWMax = "gpu_power_w_max"
    // memory
    case memUsedPct = "mem_used_pct"
    case vramUsedMb = "gpu_mem_used_mb"
    // serving
    case kvUsage = "kv"
    case genTps = "gen_tps"
    case promptTps = "prompt_tps"
    case runRequests = "run"
    case waitRequests = "wait"

    /// The group a picker shows this metric under.
    var group: ClusterHistoryMetricGroup {
        switch self {
        case .cpuTempC, .cpuTempCMax, .gpuTempC, .gpuTempCMax: return .temps
        case .gpuUtilPct, .cpuUtilPct: return .utilisation
        case .gpuPowerW, .gpuPowerWMax: return .power
        case .memUsedPct, .vramUsedMb: return .memory
        case .kvUsage, .genTps, .promptTps, .runRequests, .waitRequests: return .serving
        }
    }

    /// `hosts[]` or `engines[]`.
    var seriesKind: ClusterHistorySeriesKind {
        switch self {
        case .kvUsage, .genTps, .promptTps, .runRequests, .waitRequests: return .engine
        default: return .host
        }
    }

    /// The three peak columns only exist once a bucket has been rolled up.
    var isPeakField: Bool { rawValue.hasSuffix("_max") }

    /// The field carrying THIS one's peak, or nil when the recorder keeps none.
    ///
    /// The pairing is the recorder's, not a preference: `hscc_daemon/
    /// metrics_history.py` rolls up `max(cpu_temp_c)`, `max(gpu_temp_c)` and
    /// `max(gpu_power_w)` into the three `*_max` columns and keeps no peak for
    /// anything else, which is why `max_fields` can only ever name these three.
    /// A surface asks a metric for its companion instead of guessing from a
    /// suffix, because `cpu_usage_pct` has no peak and a name-based rule would
    /// draw a chart a roll-up never produced.
    var peakCompanion: ClusterHistoryMetric? {
        switch self {
        case .cpuTempC: return .cpuTempCMax
        case .gpuTempC: return .gpuTempCMax
        case .gpuPowerW: return .gpuPowerWMax
        default: return nil
        }
    }

    /// The average a peak field belongs to — the inverse of `peakCompanion`, so
    /// a legend can label `gpu_temp_c_max` as the excursion of `gpu_temp_c`.
    var averageCompanion: ClusterHistoryMetric? {
        guard isPeakField else { return nil }
        guard let average = ClusterHistoryMetric(rawValue: String(rawValue.dropLast(4))) else {
            return nil
        }
        return average.peakCompanion == self ? average : nil
    }

    /// Whether this metric can go into a `metrics=` list for a served tier.
    /// Tier 0 is raw 2 s samples and stores no peak, and the route says so with
    /// a 400 rather than an empty column — the widget must not offer the pick.
    func requestable(atTier tier: Int) -> Bool { !isPeakField || tier >= 1 }

    /// What to tell the operator when a peak is picked on a 15 m window.
    var tierRequirement: String? {
        isPeakField ? "needs window=1h or wider (a peak exists only after a roll-up)" : nil
    }

    /// The unit to draw after `displayValue`. Percentages here are the
    /// route's own percent columns (`*_pct`); `kv` is the one field that needs
    /// the transform below to become one.
    var unit: String {
        switch self {
        case .cpuTempC, .cpuTempCMax, .gpuTempC, .gpuTempCMax: return "\u{00B0}C"
        case .gpuUtilPct, .cpuUtilPct, .memUsedPct, .kvUsage: return "%"
        case .gpuPowerW, .gpuPowerWMax: return " W"
        case .vramUsedMb: return " MB"
        case .genTps, .promptTps: return " tok/s"
        case .runRequests, .waitRequests: return ""
        }
    }

    /// What to multiply the wire value by before drawing it.
    ///
    /// `kv` is the only metric that needs it: the recorder stores vLLM's raw
    /// `vllm:kv_cache_usage_perc` gauge (hscc_daemon/cluster_live.py maps that
    /// name to `kv`), which is a 0-1 FRACTION, and the CLI's own serving table
    /// renders `100 * kv` (cluster_live.py:597). Keeping the factor here means
    /// the widget and the CLI cannot disagree about what "KV 12%" means.
    var displayFactor: Double { self == .kvUsage ? 100 : 1 }

    /// The wire value as the surface should draw it. Nil stays nil (§the route
    /// forwards the recorder's nulls untouched; "not reported" is not zero).
    func displayValue(_ wire: Double?) -> Double? {
        guard let wire else { return nil }
        return wire * displayFactor
    }

    /// The value off one row, as it arrives on the wire (no display transform).
    /// A nil here is the recorder's own "not reported".
    func value(in point: ClusterHistoryPoint) -> Double? {
        switch self {
        case .cpuTempC: return point.cpu_temp_c
        case .cpuTempCMax: return point.cpu_temp_c_max
        case .gpuTempC: return point.gpu_temp_c
        case .gpuTempCMax: return point.gpu_temp_c_max
        case .gpuUtilPct: return point.gpu_util_pct
        case .cpuUtilPct: return point.cpu_usage_pct
        case .gpuPowerW: return point.gpu_power_w
        case .gpuPowerWMax: return point.gpu_power_w_max
        case .memUsedPct: return point.mem_used_pct
        case .vramUsedMb: return point.gpu_mem_used_mb
        case .kvUsage: return point.kv
        case .genTps: return point.gen_tps
        case .promptTps: return point.prompt_tps
        case .runRequests: return point.run.map { Double($0) }
        case .waitRequests: return point.wait.map { Double($0) }
        }
    }

    /// A short label for a picker row, derived from the wire key so a rename in
    /// the recorder cannot leave a stale label behind.
    var label: String {
        let words = rawValue
            .replacingOccurrences(of: "_max", with: " max")
            .replacingOccurrences(of: "_pct", with: " %")
            .replacingOccurrences(of: "_c", with: " C")
            .replacingOccurrences(of: "_w", with: " W")
            .replacingOccurrences(of: "_mb", with: " MB")
            .replacingOccurrences(of: "_tps", with: " tok/s")
            .replacingOccurrences(of: "_", with: " ")
        return words
    }
}

// MARK: - Sample and series

/// One point of one series: an epoch second and the value the recorder stored
/// for it. `value` is optional because the recorder stores nulls — the wire
/// carries `null` for a field a node never reported, and the difference between
/// "GPU idle at 0" and "this node never answered" is the whole point of a
/// history chart.
struct ClusterHistorySample: Equatable {
    let t: TimeInterval
    let value: Double?

    init(t: TimeInterval, value: Double?) {
        self.t = t
        self.value = value
    }
}

/// The full extent of a series over EVERY point, computed before any thinning.
/// A surface that captions "peak 88 C at 14:02" reads this, never the thinned
/// array, or the caption becomes a claim about which points survived.
struct ClusterHistoryExtremes: Equatable {
    let min: Double
    let minAt: TimeInterval
    let max: Double
    let maxAt: TimeInterval
    let counted: Int

    /// First occurrence wins for both, so the timestamp is a real sample time.
    init?(_ samples: [ClusterHistorySample]) {
        var low: Double?
        var lowAt: TimeInterval?
        var high: Double?
        var highAt: TimeInterval?
        var seen = 0
        for sample in samples {
            guard let value = sample.value else { continue }
            seen += 1
            if low == nil || value < low! { low = value; lowAt = sample.t }
            if high == nil || value > high! { high = value; highAt = sample.t }
        }
        guard let low, let lowAt, let high, let highAt else { return nil }
        self.min = low
        self.minAt = lowAt
        self.max = high
        self.maxAt = highAt
        self.counted = seen
    }
}

/// One line on the chart: one metric, one identity (a host, or a node:port),
/// from ONE response — which is what fixes its tier and resolution.
struct ClusterHistorySeries: Equatable {
    let metric: ClusterHistoryMetric
    /// The recorder's own row identity: `host` for a host series,
    /// `node:port` for an engine series (the recorder separates engines by
    /// port, because one host can run two backends on two GPUs).
    let identity: String
    let tier: Int
    let resolutionS: Int
    let samples: [ClusterHistorySample]

    /// Points that carry a number.
    var readable: [ClusterHistorySample] { samples.filter { $0.value != nil } }
    var missingCount: Int { samples.count - readable.count }
    var isEmpty: Bool { samples.isEmpty }
    /// Extremes over every point, before any thinning.
    var extremes: ClusterHistoryExtremes? { ClusterHistoryExtremes(samples) }
    /// The step the server stated for this tier — the x-axis unit, and the only
    /// legitimate measure of "the next point".
    var stepSeconds: Double { Double(max(1, resolutionS)) }

    /// Oldest first, with one point per timestamp (a row that repeats a bucket
    /// is the recorder's own last word on it, so the later row wins).
    static func sortedByTime(_ samples: [ClusterHistorySample]) -> [ClusterHistorySample] {
        let ordered = samples.sorted { $0.t < $1.t }
        var collapsed: [ClusterHistorySample] = []
        for sample in ordered {
            if let last = collapsed.last, last.t == sample.t {
                collapsed[collapsed.count - 1] = sample
            } else {
                collapsed.append(sample)
            }
        }
        return collapsed
    }
}

/// A metric's lines plus the payload's own explanation for an empty answer.
///
/// The route distinguishes "this `metrics=` filter named no field of that
/// series" (`hosts_included` / `engines_included` false) from "the fleet had
/// nothing to serve", and `note` says why the tier-0 ring may be unreadable
/// from the API process. A chart must render the first as an excluded series
/// and the second as a gap with a reason — never as a flat quiet fleet, so the
/// distinction is carried on the type instead of being re-guessed at the view.
///
/// `reason` is that explanation, and it is read from `note` deliberately rather
/// than from `store.tier0_resident`. That flag is a MEASUREMENT of the 2 s ring,
/// and the route takes it only when the served tier IS 0
/// (`hscc-api/routes_cluster.py`, `tier0_resident = points is not None` inside
/// `if tier == 0`). At tiers 1-2 the key is on the wire and its value is `null` —
/// a tier the route never read cannot report residency — and even where it does
/// answer, it speaks for one server process rather than for the fleet. Neither is
/// a reason a chart can draw; `note` is.
struct ClusterHistorySeriesSet: Equatable {
    let metric: ClusterHistoryMetric
    let kind: ClusterHistorySeriesKind
    let tier: Int
    let resolutionS: Int
    /// The route's `*_included` flag said this series was filtered out.
    let excluded: Bool
    /// The route's own `note` (or `error`) — why the answer is what it is.
    let reason: String?
    let series: [ClusterHistorySeries]

    var hasData: Bool { series.contains { !$0.readable.isEmpty } }
    /// Nothing to draw because the request asked for no field of this series.
    var isEmptyBecauseExcluded: Bool { excluded && !hasData }
    /// Nothing to draw, and the reason is the payload's, not ours.
    var isEmptyBecauseNothingServed: Bool { !excluded && !hasData }
    var identities: [String] { series.map { $0.identity } }
}

// MARK: - The average/peak pairing (rule 3)

extension ClusterHistoryResponse {
    /// The peak columns THIS answer actually served, as metrics.
    ///
    /// `max_fields` is the payload's own word for it, and an empty list is a real
    /// answer rather than a missing decode: tier 0 is raw 2 s samples, which have
    /// never been rolled up, so no peak exists to name — which is also why
    /// `ClusterHistoryMetric.requestable(atTier:)` refuses a peak there. Unknown
    /// names are dropped rather than guessed; a peak column the decoder has never
    /// heard of is caught by the Python contract gate
    /// (`test_history_peak_halves_survive_the_decoder`), which reads the peaks out
    /// of `SharedModels.swift` and compares them with what the route serves.
    var servedPeakFields: Set<ClusterHistoryMetric> {
        Set((max_fields ?? []).compactMap { ClusterHistoryMetric(rawValue: $0) })
    }

    /// The lines for `metric` together with the peak lines they must be drawn
    /// with — rule 3 above, and the reason a surface asks for a PAIR rather than
    /// for an average.
    ///
    /// `peak` is nil in exactly two cases, and both of them are the payload's
    /// answer and not the caller's oversight: the recorder keeps no `*_max`
    /// column for this metric, or `max_fields` did not name it. Everywhere else a
    /// surface that drew the average alone would be hiding the excursion an
    /// operator opened the widget to see.
    ///
    /// The peak is read from the SAME response, so it cannot come from another
    /// tier: a mean and a max from different resolutions are not one chart
    /// (rule 2).
    func historyPair(for metric: ClusterHistoryMetric)
        -> (average: ClusterHistorySeriesSet, peak: ClusterHistorySeriesSet?) {
        let average = ClusterHistorySeriesSet.make(self, for: metric)
        guard let peakMetric = metric.peakCompanion,
              servedPeakFields.contains(peakMetric) else { return (average, nil) }
        return (average, ClusterHistorySeriesSet.make(self, for: peakMetric))
    }
}

// MARK: - Building series off the payload

extension ClusterHistorySeriesSet {
    /// The lines for one metric off one `GET /v1/cluster/history` answer.
    ///
    /// Host rows are grouped by `host`, engine rows by `node:port`, in sorted
    /// order so two renders of the same payload draw the same legend. Rows whose
    /// identity is missing (the route projects identity keys through, so this is
    /// only reachable from a malformed payload) are counted under `"?"` rather
    /// than dropped — dropping them would be an unreported loss of data.
    static func make(_ response: ClusterHistoryResponse,
                     for metric: ClusterHistoryMetric) -> ClusterHistorySeriesSet {
        let rows: [ClusterHistoryPoint]
        let excluded: Bool
        switch metric.seriesKind {
        case .host:
            rows = response.hosts
            excluded = response.hosts_included == false
        case .engine:
            rows = response.engines
            excluded = response.engines_included == false
        }

        var identities: [String] = []
        var buckets: [String: [ClusterHistorySample]] = [:]
        for row in rows {
            let identity: String
            switch metric.seriesKind {
            case .host:
                identity = row.host ?? "?"
            case .engine:
                identity = "\((row.node ?? "?")):\((row.port ?? "?"))"
            }
            if buckets[identity] == nil { identities.append(identity) }
            buckets[identity, default: []].append(
                ClusterHistorySample(t: TimeInterval(row.ts), value: metric.value(in: row)))
        }

        let series = identities.sorted().map { identity in
            ClusterHistorySeries(metric: metric,
                                 identity: identity,
                                 tier: response.tier,
                                 resolutionS: response.resolution_s,
                                 samples: ClusterHistorySeries.sortedByTime(
                                    buckets[identity] ?? []))
        }
        let note = response.note.flatMap { $0.isEmpty ? nil : $0 }
        return ClusterHistorySeriesSet(metric: metric,
                                       kind: metric.seriesKind,
                                       tier: response.tier,
                                       resolutionS: response.resolution_s,
                                       excluded: excluded,
                                       reason: note ?? response.error,
                                       series: series)
    }

    /// The lines for several metrics, in the order asked for (a picker keeps
    /// its own order; `allCases` is grouped by the card's five families).
    static func make(_ response: ClusterHistoryResponse,
                     for metrics: [ClusterHistoryMetric]) -> [ClusterHistorySeriesSet] {
        metrics.map { make(response, for: $0) }
    }
}

// MARK: - Pixel-width reduction that preserves extremes

/// The result of reducing one series (or one fleet envelope) to a chart width.
///
/// It reports BOTH what the data said (`trueMin`/`trueMax`, with their
/// timestamps) and what survived (`outMin`/`outMax`), so the caller can assert
/// rather than trust: `extremesPreserved` is the property the reduction exists
/// to have, and a renderer that ignores it can still print a peak the data
/// never had.
struct ClusterHistoryDownsampled: Equatable {
    /// The points to draw, oldest first. Every one of them is a point that was
    /// in the input — the reduction never invents a value.
    let points: [ClusterHistorySample]
    let sourceCount: Int
    let pixelColumns: Int
    let resolutionS: Int
    /// Seconds covered by one pixel column (the data's span over the columns).
    let columnWidthS: Double
    /// Columns that held at least one readable value.
    let filledColumns: Int
    /// Columns that held none — a gap, and a visible one.
    let gapColumns: Int
    let trueMin: Double?
    let trueMinAt: TimeInterval?
    let trueMax: Double?
    let trueMaxAt: TimeInterval?
    let outMin: Double?
    let outMax: Double?
    /// Whether the series' own first/last readable point survived: the right
    /// hand end is the "now" value a widget headlines.
    let keptFirst: Bool
    let keptLast: Bool
    /// A retained step wider than this is a gap, not a slope — `segments()`
    /// uses it, and it is one and a half expected steps (the coarser of the
    /// tier's own step and one pixel column).
    let breakAfterS: Double

    /// The whole point of the helper: thinning must not move the extremes.
    var extremesPreserved: Bool { trueMin == outMin && trueMax == outMax }
    /// True when reduction actually happened (fewer points than the input).
    var thinned: Bool { points.count < sourceCount }
    var counted: Int { points.filter { $0.value != nil }.count }

    /// The lines to draw: `points` split wherever a retained step is wider than
    /// `breakAfterS`, so a period with no reading is a GAP and not a straight
    /// line between the two nearest numbers. One segment is the ordinary case.
    func segments() -> [[ClusterHistorySample]] {
        var out: [[ClusterHistorySample]] = []
        var current: [ClusterHistorySample] = []
        for point in points {
            if let previous = current.last, point.t - previous.t > breakAfterS {
                out.append(current)
                current = []
            }
            current.append(point)
        }
        if !current.isEmpty { out.append(current) }
        return out
    }
}

enum ClusterHistoryDownsampler {
    /// Each pixel column keeps at most its own minimum and maximum, so a series
    /// that already has no more than two points per column is reduced by
    /// nothing — and reducing it anyway would only throw away real readings.
    static let pointsPerColumn = 2

    /// Reduce one series' points to `pixelWidth` columns.
    static func downsample(_ samples: [ClusterHistorySample],
                           resolutionS: Int,
                           pixelWidth: Int) -> ClusterHistoryDownsampled {
        let ordered = ClusterHistorySeries.sortedByTime(samples)
        return reduce(ordered, resolutionS: resolutionS, pixelWidth: pixelWidth)
    }

    /// Reduce one whole series.
    static func downsample(_ series: ClusterHistorySeries,
                           pixelWidth: Int) -> ClusterHistoryDownsampled {
        downsample(series.samples, resolutionS: series.resolutionS, pixelWidth: pixelWidth)
    }

    /// Reduce SEVERAL series together — the fleet envelope: one line whose
    /// per-column min/max is the whole fleet's min/max in that column.
    ///
    /// This is the only place more than one series is ever merged, and it
    /// refuses (returns nil, with the reason logged by the caller's own copy of
    /// the request) when the series disagree about metric, tier or
    /// `resolution_s`. Folding a 2 s raw sample and a 5-minute average into the
    /// same pixel column would produce a "min/max" that describes neither, and
    /// that is exactly the lie this file exists to prevent — so it is a
    /// structural refusal, not a warning.
    static func downsample(_ series: [ClusterHistorySeries],
                           pixelWidth: Int) -> ClusterHistoryDownsampled? {
        guard let first = series.first else { return nil }
        for other in series where other.metric != first.metric
            || other.tier != first.tier
            || other.resolutionS != first.resolutionS {
            return nil
        }
        let merged = series.flatMap { $0.samples }.sorted { $0.t < $1.t }
        return reduce(merged, resolutionS: first.resolutionS, pixelWidth: pixelWidth,
                      collapseRepeatedTimestamps: false)
    }

    // The shared implementation. `collapseRepeatedTimestamps` is on for a
    // single identity (a repeated bucket is the recorder's last word on that
    // bucket) and off for a fleet envelope (two hosts legitimately report the
    // same instant, and both readings are data).
    private static func reduce(_ input: [ClusterHistorySample],
                               resolutionS: Int,
                               pixelWidth: Int,
                               collapseRepeatedTimestamps: Bool = true) -> ClusterHistoryDownsampled {
        let ordered = collapseRepeatedTimestamps
            ? ClusterHistorySeries.sortedByTime(input)
            : input
        let columns = max(1, pixelWidth)
        let trueExtremes = ClusterHistoryExtremes(ordered)
        let firstReadable = ordered.first { $0.value != nil }
        let lastReadable = ordered.last { $0.value != nil }

        // The x-domain is the DATA's own span: bucketing over a wider window
        // would move real points into empty columns and shrink the peak of the
        // column that holds them.
        let low = ordered.first?.t ?? 0
        let high = ordered.last?.t ?? 0
        let span = high - low
        let columnWidth = span > 0 ? span / Double(columns) : 1

        func column(of t: TimeInterval) -> Int {
            let raw = Int((t - low) / columnWidth)
            if raw < 0 { return 0 }
            if raw > columns - 1 { return columns - 1 }
            return raw
        }

        var filled: Set<Int> = []
        var bucketMin: [Int: ClusterHistorySample] = [:]
        var bucketMax: [Int: ClusterHistorySample] = [:]
        for sample in ordered {
            guard let value = sample.value else { continue }
            let index = column(of: sample.t)
            filled.insert(index)
            if let current = bucketMin[index], let currentV = current.value, currentV <= value {
                // keep the earlier occurrence of an equal minimum
            } else {
                bucketMin[index] = sample
            }
            if let current = bucketMax[index], let currentV = current.value, currentV >= value {
                // keep the earlier occurrence of an equal maximum
            } else {
                bucketMax[index] = sample
            }
        }

        let gapColumns = columns - filled.count
        let step = Double(max(1, resolutionS))
        let result: ClusterHistoryDownsampled
        if ordered.count <= columns * pointsPerColumn {
            // Nothing to reduce: draw what was sent, so a short series is
            // never made uglier by a helper that had nothing to do.
            result = build(points: ordered, source: ordered, columns: columns,
                           resolutionS: resolutionS, columnWidth: columnWidth,
                           filled: filled.count, gaps: gapColumns,
                           extremes: trueExtremes, first: firstReadable, last: lastReadable,
                           step: step)
        } else {
            var selected: [ClusterHistorySample] = []
            for index in filled.sorted() {
                if let minPoint = bucketMin[index] { selected.append(minPoint) }
                if let maxPoint = bucketMax[index] { selected.append(maxPoint) }
            }
            // The ends of the line are load-bearing: the right-hand end is the
            // "now" value a widget headlines, so it must survive even if its
            // column's extremes were someone else's.
            if let firstReadable { selected.append(firstReadable) }
            if let lastReadable { selected.append(lastReadable) }
            result = build(points: selected,
                           source: ordered, columns: columns, resolutionS: resolutionS,
                           columnWidth: columnWidth, filled: filled.count, gaps: gapColumns,
                           extremes: trueExtremes, first: firstReadable, last: lastReadable,
                           step: step)
        }
        return result
    }

    private static func build(points: [ClusterHistorySample],
                              source: [ClusterHistorySample],
                              columns: Int,
                              resolutionS: Int,
                              columnWidth: Double,
                              filled: Int,
                              gaps: Int,
                              extremes: ClusterHistoryExtremes?,
                              first: ClusterHistorySample?,
                              last: ClusterHistorySample?,
                              step: Double) -> ClusterHistoryDownsampled {
        // A point may legitimately be duplicated (a column's min can be the
        // series' first point), and two points may share a timestamp when the
        // reduction covers a fleet (two hosts legitimately report the same
        // second). So: sort deterministically on (time, value) — never drop one
        // of a same-time pair, or a column's maximum could lose to its own
        // minimum — then drop only EXACT duplicates so the polyline has one
        // vertex per reading.
        let sorted = points.sorted { a, b in
            if a.t != b.t { return a.t < b.t }
            let av = a.value ?? .greatestFiniteMagnitude
            let bv = b.value ?? .greatestFiniteMagnitude
            return av < bv
        }
        var unique: [ClusterHistorySample] = []
        for point in sorted {
            if let previous = unique.last, previous.t == point.t, previous.value == point.value {
                continue
            }
            unique.append(point)
        }
        let drawn = ClusterHistoryExtremes(unique)
        return ClusterHistoryDownsampled(
            points: unique,
            sourceCount: source.count,
            pixelColumns: columns,
            resolutionS: resolutionS,
            columnWidthS: columnWidth,
            filledColumns: filled,
            gapColumns: max(0, gaps),
            trueMin: extremes?.min,
            trueMinAt: extremes?.minAt,
            trueMax: extremes?.max,
            trueMaxAt: extremes?.maxAt,
            outMin: drawn?.min,
            outMax: drawn?.max,
            keptFirst: first.map { f in unique.contains { $0.t == f.t && $0.value == f.value } } ?? true,
            keptLast: last.map { l in unique.contains { $0.t == l.t && $0.value == l.value } } ?? true,
            breakAfterS: max(step, columnWidth) * 1.5)
    }
}
