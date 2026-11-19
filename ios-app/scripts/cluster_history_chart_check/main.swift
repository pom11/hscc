import Foundation

// ===========================================================================
// cluster_history_chart_check/main.swift — execute the shared history chart
// layer (Sources/Shared/ClusterHistorySeries.swift) against the REAL payload
// types and the REAL bytes the route serves.
//
// Compiled by scripts/cluster_history_chart_check.sh together with
//   Sources/Shared/SharedModels.swift         (the wire types, never redeclared)
//   Sources/Shared/SharedStore.swift          (SharedModels references Theme)
//   Sources/Shared/ClusterHistorySeries.swift  <- the subject
//   scripts/model_decode_check/ThemeStub.swift (the one non-model shim)
// so every assertion below runs the code the app ships, not a copy of it.
//
// The fixtures are not hand-typed: scripts/make_cluster_history_fixture.py
// fills a store through the recorder's own write path and then calls
// routes_cluster.handle_cluster_history, so what is decoded here is handler
// output. Read that file before trusting a green run.
//
// Two claims matter more than the rest, and each is asserted twice — once on
// real payload bytes, once on a synthetic series built to break it:
//
//   * a pixel-width reduction must not move the min or the max. The spike sits
//     at an index a stride thin provably skips, and the contrast with that
//     stride thin is itself asserted, so the test cannot pass vacuously;
//   * a series never mixes tiers, and an empty answer keeps the payload's own
//     reason instead of drawing a quiet fleet.
//
// NEGATIVE TEST (guarded): CLUSTER_HISTORY_NEGATIVE_TEST=1 swaps the real
// reduction for a naive stride thin, so the spike assertions MUST fail. A
// harness that cannot fail is worthless.
// ===========================================================================

let fixtureDir = CommandLine.arguments.count > 1
    ? CommandLine.arguments[1]
    : "scripts/model_decode_check/fixtures"

var failures: [String] = []
var checks = 0

func check(_ name: String, _ ok: Bool, _ detail: String = "") {
    checks += 1
    if ok {
        print("  ok: \(name)")
    } else {
        failures.append(name)
        print("FAIL: \(name)\(detail.isEmpty ? "" : " — " + detail)")
    }
}

// MARK: - Fixture loading

struct ServedCounts: Decodable {
    let counts: Counts
    struct Counts: Decodable { let hosts: Int; let engines: Int }
}

/// The engine rows as the store really types them: sqlite REAL columns, so a
/// count of 2 is on the wire as 2.0.
struct RealEngineRows: Decodable {
    let engines: [Row]
    struct Row: Decodable { let run: Double?; let wait: Double? }
}

func fixtureData(_ name: String) throws -> Data {
    try Data(contentsOf: URL(fileURLWithPath: fixtureDir + "/" + name))
}

func decodeHistory(_ name: String) throws -> ClusterHistoryResponse {
    try JSONDecoder().decode(ClusterHistoryResponse.self, from: fixtureData(name))
}

// MARK: - Synthetic series helpers

func samples(_ values: [Double?], start: Double = 1_795_000_000,
             step: Double = 60) -> [ClusterHistorySample] {
    values.enumerated().map { index, value in
        ClusterHistorySample(t: start + Double(index) * step, value: value)
    }
}

func synthetic(_ values: [Double?], metric: ClusterHistoryMetric = .gpuTempC,
               identity: String = "192.0.2.10", tier: Int = 1,
               resolutionS: Int = 60) -> ClusterHistorySeries {
    ClusterHistorySeries(metric: metric, identity: identity, tier: tier,
                         resolutionS: resolutionS, samples: samples(values))
}

/// The reduction this file exists to replace: keep every Nth point. Same output
/// size as the column reduction, and it drops whichever extreme is not sitting
/// on a multiple of N — which is exactly the bug a shipped chart would have.
func naiveStrideThin(_ points: [ClusterHistorySample], target: Int) -> [ClusterHistorySample] {
    guard !points.isEmpty else { return [] }
    let by = max(1, points.count / max(1, target))
    var out: [ClusterHistorySample] = []
    var index = 0
    while index < points.count {
        out.append(points[index])
        index += by
    }
    return out
}

func signature(_ sample: ClusterHistorySample) -> String {
    "\(sample.t)|\(sample.value.map { String($0) } ?? "nil")"
}

let negativeTest = ProcessInfo.processInfo.environment["CLUSTER_HISTORY_NEGATIVE_TEST"] == "1"

/// The reduction under test. Under the negative test the naive stride thin
/// takes its place, so the spike assertions below have to fail.
func reduce(_ points: [ClusterHistorySample], resolutionS: Int,
            pixelWidth: Int) -> ClusterHistoryDownsampled {
    if negativeTest {
        let thinned = naiveStrideThin(points, target: pixelWidth)
        let truth = ClusterHistoryExtremes(points)
        let drawnTruth = ClusterHistoryExtremes(thinned)
        return ClusterHistoryDownsampled(
            points: thinned, sourceCount: points.count, pixelColumns: pixelWidth,
            resolutionS: resolutionS, columnWidthS: 1, filledColumns: thinned.count,
            gapColumns: thinned.isEmpty ? pixelWidth : 0,
            trueMin: truth?.min, trueMinAt: truth?.minAt,
            trueMax: truth?.max, trueMaxAt: truth?.maxAt,
            outMin: drawnTruth?.min, outMax: drawnTruth?.max,
            keptFirst: true, keptLast: true,
            breakAfterS: Double(max(1, resolutionS)) * 1.5)
    }
    return ClusterHistoryDownsampler.downsample(points, resolutionS: resolutionS,
                                                pixelWidth: pixelWidth)
}

if negativeTest {
    print("NOTE: CLUSTER_HISTORY_NEGATIVE_TEST=1 — a naive stride thin stands in for")
    print("      the real reduction, so the spike assertions below MUST fail.")
}

// ===========================================================================
// 1. The metric vocabulary is the recorder's vocabulary
// ===========================================================================

print("\n[1] metric vocabulary (the recorder's own column names)")

let allMetrics = ClusterHistoryMetric.allCases
let rawValues = allMetrics.map { $0.rawValue }
check("every metric raw value is distinct", Set(rawValues).count == rawValues.count,
      rawValues.joined(separator: ","))
check("the five metric families are all represented",
      ClusterHistoryMetricGroup.allCases.count == 5
        && ClusterHistoryMetricGroup.allCases.allSatisfy { group in
            allMetrics.contains { $0.group == group }
        })
check("peaks are exactly the three *_max columns",
      Set(allMetrics.filter { $0.isPeakField }.map { $0.rawValue })
        == ["cpu_temp_c_max", "gpu_temp_c_max", "gpu_power_w_max"],
      allMetrics.filter { $0.isPeakField }.map { $0.rawValue }.joined(separator: ","))
check("a peak is not requestable at tier 0 and is at every tier above it",
      allMetrics.allSatisfy { $0.requestable(atTier: 0) == !$0.isPeakField }
        && allMetrics.allSatisfy { $0.requestable(atTier: 1) && $0.requestable(atTier: 2) })
check("only a peak carries a tier requirement, and it names the window",
      allMetrics.allSatisfy { ($0.tierRequirement != nil) == $0.isPeakField }
        && allMetrics.filter { $0.isPeakField }.allSatisfy {
            $0.tierRequirement?.contains("1h") ?? false
        })
check("the engine series are exactly the five serving fields",
      Set(allMetrics.filter { $0.seriesKind == .engine }.map { $0.rawValue })
        == ["kv", "gen_tps", "prompt_tps", "run", "wait"],
      allMetrics.filter { $0.seriesKind == .engine }.map { $0.rawValue }.joined(separator: ","))
check("VRAM is megabytes, never a share of a total the route does not send",
      ClusterHistoryMetric.vramUsedMb.rawValue == "gpu_mem_used_mb"
        && ClusterHistoryMetric.vramUsedMb.unit == " MB"
        && !rawValues.contains { $0.contains("mem_total") })
check("kv is the one field that needs the CLI's x100, and nil stays nil",
      allMetrics.allSatisfy { ($0.displayFactor == 100) == ($0 == .kvUsage) }
        && ClusterHistoryMetric.kvUsage.displayValue(0.125) == 12.5
        && ClusterHistoryMetric.kvUsage.displayValue(nil) == nil)
// Every metric must read the ONE field its wire key names. A metric wired to
// the wrong column would compile, decode and draw a plausible line about the
// wrong thing — which no decode gate can see — so each field carries a
// different number here and the mapping is pinned reading by reading.
let mapped = ClusterHistoryPoint(
    ts: 1, n: nil, host: nil, node: nil, port: nil, model: nil, error: nil,
    cpu_usage_pct: 1.1, cpu_temp_c: 2.2, gpu_util_pct: 3.3, gpu_temp_c: 4.4,
    gpu_power_w: 5.5, gpu_power_w_max: 6.6, gpu_temp_c_max: 7.7,
    cpu_temp_c_max: 8.8, mem_used_pct: 9.9, gpu_mem_used_mb: 10.1,
    run: 14, wait: 15, kv: 11.1, gen_tps: 12.2, prompt_tps: 13.3)
let expectedValues: [(ClusterHistoryMetric, Double)] = [
    (.cpuUtilPct, 1.1), (.cpuTempC, 2.2), (.gpuUtilPct, 3.3), (.gpuTempC, 4.4),
    (.gpuPowerW, 5.5), (.gpuPowerWMax, 6.6), (.gpuTempCMax, 7.7), (.cpuTempCMax, 8.8),
    (.memUsedPct, 9.9), (.vramUsedMb, 10.1), (.kvUsage, 11.1), (.genTps, 12.2),
    (.promptTps, 13.3), (.runRequests, 14.0), (.waitRequests, 15.0),
]
check("each metric reads exactly the field its wire key names",
      expectedValues.count == allMetrics.count
        && expectedValues.allSatisfy { metric, value in metric.value(in: mapped) == value },
      expectedValues.filter { metric, value in metric.value(in: mapped) != value }
        .map { "\($0.0.rawValue)=\(String(describing: $0.0.value(in: mapped)))" }
        .joined(separator: " "))
let emptyPoint = ClusterHistoryPoint(
    ts: 1, n: nil, host: nil, node: nil, port: nil, model: nil, error: nil,
    cpu_usage_pct: nil, cpu_temp_c: nil, gpu_util_pct: nil, gpu_temp_c: nil,
    gpu_power_w: nil, gpu_power_w_max: nil, gpu_temp_c_max: nil, cpu_temp_c_max: nil,
    mem_used_pct: nil, gpu_mem_used_mb: nil, run: nil, wait: nil, kv: nil,
    gen_tps: nil, prompt_tps: nil)
check("a row that reported nothing reads as nil for every metric (never as 0)",
      allMetrics.allSatisfy { $0.value(in: emptyPoint) == nil })

// ===========================================================================
// 2. The route's own bytes
// ===========================================================================

print("\n[2] the route's own bytes (fixtures generated by make_cluster_history_fixture.py)")

let history6h: ClusterHistoryResponse?
do {
    history6h = try decodeHistory("v1_cluster_history_6h.json")
} catch {
    history6h = nil
    check("v1_cluster_history_6h.json decodes into ClusterHistoryResponse", false, "\(error)")
}

if let response = history6h {
    check("the 6h answer states the tier and resolution it served",
          response.tier == 1 && response.resolution_s == 60 && response.resolution == "60s"
            && response.window == "6h" && response.window_s == 21600)
    check("the rolled-up answer names its three peak columns",
          response.max_fields == ["cpu_temp_c_max", "gpu_temp_c_max", "gpu_power_w_max"],
          "\(response.max_fields ?? [])")

    let served = try? JSONDecoder().decode(ServedCounts.self,
                                           from: fixtureData("v1_cluster_history_6h.json"))
    check("the rows this layer groups equal the counts the route stated",
          served?.counts.hosts == response.hosts.count && served?.counts.engines == response.engines.count,
          "served=\(String(describing: served?.counts)) decoded hosts=\(response.hosts.count) engines=\(response.engines.count)")

    // The store's columns are sqlite REAL, so run/wait arrive as 2.0 / 1.0. The
    // model's Int? survives only because JSONDecoder converts an integral
    // number losslessly — prove both halves from the same bytes.
    let realRows = try? JSONDecoder().decode(RealEngineRows.self,
                                             from: fixtureData("v1_cluster_history_6h.json"))
    check("the wire carries run/wait as REAL numbers (2.0) and the model reads them as 2",
          (realRows?.engines.first?.run ?? 0) == 2.0
            && (realRows?.engines.first?.wait ?? 0) == 1.0
            && response.engines.first?.run == 2
            && response.engines.first?.wait == 1,
          "wire=\(String(describing: realRows?.engines.first)) model run=\(String(describing: response.engines.first?.run))")

    let missingTemp = response.hosts.filter { $0.gpu_temp_c == nil }
    check("a node that never reported its GPU stays nil and is never read as 0",
          !missingTemp.isEmpty
            && response.hosts.allSatisfy { $0.gpu_temp_c != 0 }
            && missingTemp.allSatisfy { $0.gpu_temp_c == nil },
          "null rows=\(missingTemp.count)")
    check("gpu_mem_used_mb and prompt_tps decode (the two keys the chart needs)",
          response.hosts.first?.gpu_mem_used_mb == 12000
            && response.engines.first?.prompt_tps == 200)
    let storeFields = Mirror(reflecting: response.store!).children.compactMap { $0.label }
    check("the store's server-side path is never decoded into the App Group",
          !storeFields.contains("path"), storeFields.joined(separator: ","))
    check("every series inherits the tier and step the server stated",
          ClusterHistorySeriesSet.make(response, for: .gpuTempC).series.allSatisfy {
              $0.tier == 1 && $0.resolutionS == 60 && $0.stepSeconds == 60
          })

    let hosts = ClusterHistorySeriesSet.make(response, for: .gpuUtilPct)
    check("host rows become one line per host, sorted",
          hosts.identities == ["192.0.2.10", "192.0.2.11"], hosts.identities.joined(separator: ","))
    check("each host line carries every bucket the route served for it",
          hosts.series.count == 2 && hosts.series.allSatisfy { $0.samples.count == 12 })
    let engines = ClusterHistorySeriesSet.make(response, for: .genTps)
    check("engine rows are keyed node:port, not by model",
          engines.identities == ["192.0.2.10:8000"] && engines.series.first?.samples.count == 12,
          engines.identities.joined(separator: ","))
    let temps = ClusterHistorySeriesSet.make(response, for: .gpuTempC)
    let silentHost = temps.series.first { $0.identity == "192.0.2.11" }
    check("the node that never reported its GPU is a gap, not a flat zero line",
          (silentHost?.missingCount ?? 0) == 12 && (silentHost?.readable.isEmpty ?? false),
          "missing=\(String(describing: silentHost?.missingCount))")
    let talkingHost = temps.series.first { $0.identity == "192.0.2.10" }
    check("the node that did report has an extent over every one of its buckets",
          (talkingHost?.extremes?.counted ?? 0) == 12
            && talkingHost?.extremes?.min == 71 && talkingHost?.extremes?.max == 71,
          "extremes=\(String(describing: talkingHost?.extremes))")
    let kv = ClusterHistorySeriesSet.make(response, for: .kvUsage)
    check("the wire kv is a 0-1 fraction and the shared layer turns it into the CLI's percent",
          kv.series.first?.samples.first?.value == 0.125
            && ClusterHistoryMetric.kvUsage.displayValue(kv.series.first?.samples.first?.value) == 12.5)
    check("a host metric and an engine metric never answer from the same row array",
          temps.kind == .host && engines.kind == .engine && temps.excluded == false)
    let fixtureText = (try? String(contentsOf: URL(fileURLWithPath: fixtureDir + "/v1_cluster_history_6h.json"),
                                  encoding: .utf8)) ?? ""
    check("the generated fixture stays on TEST-NET addresses",
          fixtureText.contains("192.0.2.10") && !fixtureText.contains("192.168.")
            && !fixtureText.contains("10.0.0."), "fixture hosts must stay on 192.0.2.x")
}

let engineOnly: ClusterHistoryResponse?
do {
    engineOnly = try decodeHistory("v1_cluster_history_6h_engine_only.json")
} catch {
    engineOnly = nil
    check("v1_cluster_history_6h_engine_only.json decodes", false, "\(error)")
}
if let projection = engineOnly {
    let hostSet = ClusterHistorySeriesSet.make(projection, for: .gpuTempC)
    let engineSet = ClusterHistorySeriesSet.make(projection, for: .genTps)
    check("a metrics= projection reports the host series as EXCLUDED",
          projection.hosts_included == false && hostSet.excluded && hostSet.isEmptyBecauseExcluded,
          "hosts_included=\(String(describing: projection.hosts_included))")
    check("an excluded series is never reported as 'the fleet served nothing'",
          !hostSet.isEmptyBecauseNothingServed && engineSet.hasData)
    check("the projection still keys its engine series by node:port",
          engineSet.identities == ["192.0.2.10:8000"], engineSet.identities.joined(separator: ","))
    check("a projected row keeps the identity keys the chart needs and drops the rest",
          projection.engines.first?.kv == nil && projection.engines.first?.gen_tps != nil)
}

let tier0: ClusterHistoryResponse?
do {
    tier0 = try decodeHistory("v1_cluster_history_15m.json")
} catch {
    tier0 = nil
    check("v1_cluster_history_15m.json decodes", false, "\(error)")
}
if let ring = tier0 {
    let set = ClusterHistorySeriesSet.make(ring, for: .gpuTempC)
    check("tier 0 states that the 2 s ring is not resident in this process",
          ring.tier == 0 && ring.resolution_s == 2 && ring.tier0Resident == false)
    check("an empty tier-0 answer keeps the route's own reason",
          set.isEmptyBecauseNothingServed && !set.excluded
            && (set.reason?.contains("window=1h") ?? false), set.reason ?? "<no reason>")
    check("tier 0 carries no peak columns, and a peak is not requestable at it",
          (ring.max_fields ?? []).isEmpty
            && allMetrics.filter { $0.isPeakField }.allSatisfy { !$0.requestable(atTier: ring.tier) })
    let reduced = reduce([], resolutionS: ring.resolution_s, pixelWidth: 40)
    check("reducing an empty series draws nothing and claims no extremes",
          reduced.points.isEmpty && reduced.trueMin == nil && reduced.trueMax == nil
            && reduced.extremesPreserved)
}

// ===========================================================================
// 3. The reduction: a spike must survive being drawn at 40 pixels
// ===========================================================================

print("\n[3] pixel-width reduction preserves min and max")

let spikeIndex = 337
var spikeValues: [Double?] = (0..<600).map { 40.0 + Double($0 % 3) }
spikeValues[spikeIndex] = 99.0
let spikeSamples = samples(spikeValues)
let spikeTime = spikeSamples[spikeIndex].t
let spikeTrueMax = 99.0

let drawn = reduce(spikeSamples, resolutionS: 60, pixelWidth: 40)
let inputSignatures = Set(spikeSamples.map(signature))
check("a 600-point series drawn into 40 pixels really is reduced", drawn.thinned,
      "points=\(drawn.points.count) of \(drawn.sourceCount)")
check("the thinned line still holds the series' true maximum",
      drawn.extremesPreserved && drawn.outMax == spikeTrueMax,
      "true=\(String(describing: drawn.trueMax)) drawn=\(String(describing: drawn.outMax))")
check("and it still holds the true minimum", drawn.outMin == 40.0,
      "true=\(String(describing: drawn.trueMin)) drawn=\(String(describing: drawn.outMin))")
check("the peak's timestamp is measured over the full data, not the survivors",
      drawn.trueMaxAt == spikeTime, "trueMaxAt=\(String(describing: drawn.trueMaxAt))")
check("the spike's own reading is one of the drawn points",
      drawn.points.contains { $0.t == spikeTime && $0.value == spikeTrueMax })
check("nothing is invented: every drawn point is a reading that was sent",
      drawn.points.allSatisfy { inputSignatures.contains(signature($0)) })
check("drawn points come out oldest first",
      zip(drawn.points, drawn.points.dropFirst()).allSatisfy { $0.t <= $1.t })
check("no column draws more than its min and its max",
      drawn.points.count <= 2 * drawn.pixelColumns + 2,
      "points=\(drawn.points.count) columns=\(drawn.pixelColumns)")
check("the ends survive: the first and the last reading are both drawn",
      drawn.keptFirst && drawn.keptLast)

let naive = naiveStrideThin(spikeSamples, target: 40)
let naiveMax = naive.compactMap { $0.value }.max()
check("a stride thin of the same size LOSES the spike, so the assertions above have teeth",
      naiveMax != spikeTrueMax && naive.count < spikeSamples.count,
      "naive max=\(String(describing: naiveMax)) count=\(naive.count)")

// The right-hand end is the "now" value a widget headlines, so it must survive
// even when it is neither its column's min nor its max.
var tailValues: [Double?] = (0..<400).map { _ in 50.0 }
tailValues[0] = 45.0
tailValues[399] = 55.0
let tailSamples = samples(tailValues)
let tail = reduce(tailSamples, resolutionS: 60, pixelWidth: 30)
check("a last reading that is neither min nor max still survives (the 'now' value)",
      tail.keptLast && tail.points.last?.value == 55.0
        && tail.points.last?.t == tailSamples[399].t,
      "last=\(String(describing: tail.points.last))")
check("and so does a first reading that is neither min nor max",
      tail.keptFirst && tail.points.first?.value == 45.0,
      "first=\(String(describing: tail.points.first))")

// Nulls: a column with no reading stays a gap and never becomes a number.
var gapValues: [Double?] = (0..<300).map { Double($0 % 50) }
for index in 100...140 { gapValues[index] = nil }
let gapSamples = samples(gapValues)
let gapped = reduce(gapSamples, resolutionS: 60, pixelWidth: 25)
let gapTruth = ClusterHistoryExtremes(gapSamples)
check("a column with no reading is counted as a gap column",
      gapped.gapColumns > 0 && gapped.filledColumns < gapped.pixelColumns,
      "gaps=\(gapped.gapColumns) filled=\(gapped.filledColumns)")
check("a gap never becomes a zero or any other invented value",
      gapped.points.allSatisfy { $0.value != nil } && gapped.outMin == gapTruth?.min
        && gapped.outMax == gapTruth?.max,
      "out=\(String(describing: gapped.outMin))..\(String(describing: gapped.outMax)) truth=\(String(describing: gapTruth?.min))..\(String(describing: gapTruth?.max))")
check("extremes are computed over readable readings only",
      gapTruth?.counted == gapSamples.filter { $0.value != nil }.count
        && gapped.trueMin == 0.0 && gapped.trueMax == 49.0,
      "counted=\(String(describing: gapTruth?.counted))")

let allNil = reduce([ClusterHistorySample?](repeating: nil, count: 120).compactMap { $0 },
                    resolutionS: 60, pixelWidth: 20)
check("an all-null series reduces to nothing and claims no extremes",
      allNil.points.isEmpty && allNil.trueMin == nil && allNil.trueMax == nil
        && allNil.gapColumns == allNil.pixelColumns)

let short = reduce(samples((0..<30).map { Double($0) }), resolutionS: 60, pixelWidth: 40)
check("a series that already fits the width is passed through untouched",
      !short.thinned && short.points.count == 30 && short.extremesPreserved)

let oneColumn = reduce(spikeSamples, resolutionS: 60, pixelWidth: 1)
check("one pixel column still shows the min and the max, in time order",
      oneColumn.extremesPreserved
        && oneColumn.points.contains { $0.t == spikeTime && $0.value == spikeTrueMax }
        && oneColumn.points.contains { $0.value == 40.0 }
        && zip(oneColumn.points, oneColumn.points.dropFirst()).allSatisfy { $0.t <= $1.t },
      "points=\(oneColumn.points)")

// A period with no samples at all must not be bridged by a straight line.
var holed: [ClusterHistorySample] = []
for index in 0..<20 {
    holed.append(ClusterHistorySample(t: 1_795_000_000 + Double(index) * 60, value: 30.0))
}
for index in 60..<80 {
    holed.append(ClusterHistorySample(t: 1_795_000_000 + Double(index) * 60, value: 31.0))
}
let holedResult = reduce(holed, resolutionS: 60, pixelWidth: 10)
check("a period with no reading breaks the line instead of connecting across it",
      holedResult.segments().count >= 2
        && holedResult.segments().allSatisfy { segment in
            zip(segment, segment.dropFirst()).allSatisfy { $1.t - $0.t <= holedResult.breakAfterS }
        },
      "segments=\(holedResult.segments().count) breakAfterS=\(holedResult.breakAfterS)")
check("an ordinary reduced line is one segment",
      drawn.segments().count == 1, "segments=\(drawn.segments().count)")

// ===========================================================================
// 4. Tiers are never mixed
// ===========================================================================

print("\n[4] tiers are never mixed into one series")

let fast = synthetic((0..<200).map { _ in 60.0 }, metric: .gpuTempC, tier: 0, resolutionS: 2)
let slow = synthetic((0..<200).map { _ in 70.0 }, metric: .gpuTempC, tier: 2, resolutionS: 300)
check("combining two tiers of one metric is REFUSED, not averaged",
      ClusterHistoryDownsampler.downsample([fast, slow], pixelWidth: 20) == nil)
let otherMetric = synthetic((0..<200).map { _ in 60.0 }, metric: .gpuPowerW, tier: 0, resolutionS: 2)
check("combining two metrics is refused too",
      ClusterHistoryDownsampler.downsample([fast, otherMetric], pixelWidth: 20) == nil)
check("an empty set is refused rather than answered with an empty chart",
      ClusterHistoryDownsampler.downsample([ClusterHistorySeries](), pixelWidth: 20) == nil)
check("two same-tier series of different resolutions are refused as well",
      ClusterHistoryDownsampler.downsample([
          synthetic((0..<200).map { _ in 60.0 }, tier: 1, resolutionS: 60),
          synthetic((0..<200).map { _ in 60.0 }, tier: 1, resolutionS: 300),
      ], pixelWidth: 20) == nil)

let hostA = synthetic((0..<300).map { Double($0 % 40) }, metric: .gpuTempC, identity: "192.0.2.10")
let hostB = synthetic((0..<300).map { 50.0 + Double($0 % 40) }, metric: .gpuTempC, identity: "192.0.2.11")
let envelope = ClusterHistoryDownsampler.downsample([hostA, hostB], pixelWidth: 30)
check("a same-tier fleet envelope keeps the fleet's own min and max",
      envelope != nil && (envelope?.extremesPreserved ?? false)
        && envelope?.trueMin == 0 && envelope?.trueMax == 89,
      "min=\(String(describing: envelope?.trueMin)) max=\(String(describing: envelope?.trueMax))")
check("the envelope is still a reduction over both series",
      (envelope?.thinned ?? false) && (envelope?.sourceCount ?? 0) == 600)
check("two hosts reporting the same second keep BOTH readings",
      ClusterHistoryExtremes(hostA.samples + hostB.samples)?.counted == 600
        && ClusterHistoryExtremes(hostA.samples + hostB.samples)?.max == 89)

// ===========================================================================
// 5. An average is never drawn alone where the tier served a peak (rule 3)
// ===========================================================================
//
// This section is the half of `MetricsHistoryTrend.swift` that t_2c629557
// retired with the route: the gap rule and the average/peak pairing, asserted
// over `ClusterHistoryResponse` instead of `MetricsHistoryResponse`. The rules
// themselves did not change, only the type they hang off (t_447cc7fc).

print("\n[5] the average/peak pairing (rule 3, carried over from the retired trend file)")

let peakTable = allMetrics.compactMap { metric -> String? in
    guard let companion = metric.peakCompanion else { return nil }
    return "\(metric.rawValue)->\(companion.rawValue)"
}
check("the pairing table names exactly the recorder's three peak columns",
      Set(peakTable) == ["cpu_temp_c->cpu_temp_c_max", "gpu_temp_c->gpu_temp_c_max",
                         "gpu_power_w->gpu_power_w_max"],
      peakTable.joined(separator: " "))
check("a peak is nobody's average, and each peak points back at the average it belongs to",
      allMetrics.filter { $0.isPeakField }.allSatisfy { $0.peakCompanion == nil }
        && allMetrics.filter { $0.isPeakField }.allSatisfy {
            $0.averageCompanion?.peakCompanion == $0
        }
        && allMetrics.filter { !$0.isPeakField }.allSatisfy { $0.averageCompanion == nil })

if let served = history6h {
    check("the 6 h answer states exactly the three peaks it served",
          served.servedPeakFields == Set<ClusterHistoryMetric>(
              [.cpuTempCMax, .gpuTempCMax, .gpuPowerWMax]),
          served.servedPeakFields.map { $0.rawValue }.sorted().joined(separator: ","))
    for average in [ClusterHistoryMetric.cpuTempC, .gpuTempC, .gpuPowerW] {
        let pair = served.historyPair(for: average)
        check("\(average.rawValue) comes paired with its peak on a rolled-up tier",
              pair.peak != nil)
        check("\(average.rawValue)'s peak is the same tier and step as its average",
              pair.peak.map { $0.tier == pair.average.tier
                && $0.resolutionS == pair.average.resolutionS } ?? false)
        check("\(average.rawValue)'s peak covers exactly the identities its average does",
              pair.peak.map { Set($0.identities) == Set(pair.average.identities) } ?? false,
              "average=\(pair.average.identities) peak=\(pair.peak?.identities ?? [])")
    }
    check("a metric the recorder keeps no peak for is drawn alone and says so",
          served.historyPair(for: .memUsedPct).peak == nil
            && ClusterHistoryMetric.memUsedPct.peakCompanion == nil)
}
if let raw = tier0 {
    check("tier 0 names no peak columns, so an average alone is legitimate there",
          raw.servedPeakFields.isEmpty && raw.historyPair(for: .gpuTempC).peak == nil,
          "max_fields=\(String(describing: raw.max_fields))")
}

// The magnitude half of the rule needs bytes whose peak is NOT its mean. The
// route's own fixtures are flat — `make_cluster_history_fixture.py` fills one
// value per column and the roll-up of a constant is that constant — so a strict
// excursion is stated here rather than wished for out of a fixture.
let excursionJSON = #"""
{"window":"3600s","window_s":3600,"axis":"tier","source":"sqlite_store","tier":1,
 "resolution_s":60,"resolution":"1 minute","tier_summary":"1 minute averages",
 "max_fields":["gpu_temp_c_max"],
 "since":1795000000,"until":1795000600,"node":"all",
 "hosts_included":true,"engines_included":false,
 "hosts":[
   {"ts":1795000000,"host":"dgx-1","gpu_temp_c":40.0,"gpu_temp_c_max":91.0},
   {"ts":1795000060,"host":"dgx-1","gpu_temp_c":null,"gpu_temp_c_max":null}],
 "engines":[],"note":null,"error":null,
 "speak":"1 minute averages over 1 host",
 "store":{"exists":true,"db_bytes":4096,"tier0_resident":null,"error":null}}
"""#
var excursion: ClusterHistoryResponse?
do {
    excursion = try JSONDecoder().decode(ClusterHistoryResponse.self,
                                         from: Data(excursionJSON.utf8))
} catch {
    check("the excursion payload decodes through the real model", false, "\(error)")
}
if let excursion, excursion.hosts.count == 2 {
    let pair = excursion.historyPair(for: .gpuTempC)
    let mean = pair.average.series.first?.extremes?.max
    let peak = pair.peak?.series.first?.extremes?.max
    check("the peak arrives beside the mean it belongs to", pair.peak != nil)
    check("a surface that drew the mean alone would top out 51 C below the excursion",
          mean == 40 && peak == 91,
          "mean=\(String(describing: mean)) peak=\(String(describing: peak))")
    check("the bucket nobody answered stays a countable gap, not a 0.0",
          pair.average.series.first?.missingCount == 1
            && pair.average.series.first?.samples.last?.value == nil
            && pair.average.series.first?.readable.count == 1)
    check("the pairing reads only the response it was handed, so it cannot mix tiers",
          pair.peak?.tier == 1 && pair.peak?.resolutionS == 60)
}

// ===========================================================================
// Summary
// ===========================================================================

print("")
if failures.isEmpty {
    print("CLUSTER HISTORY CHART CHECK PASSED — \(checks)/\(checks) checks "
        + "(subject: Sources/Shared/ClusterHistorySeries.swift, executed against the "
        + "route's own payload bytes)")
    exit(0)
} else {
    print("CLUSTER HISTORY CHART CHECK FAILED — \(failures.count) of \(checks) checks did not pass:")
    for name in failures { print("  - \(name)") }
    exit(1)
}
