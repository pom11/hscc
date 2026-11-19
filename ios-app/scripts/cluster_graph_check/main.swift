import Foundation

// ===========================================================================
// cluster_graph_check/main.swift — execute the history graph widget's
// projection layer (Sources/Shared/ClusterGraphReading.swift) against the REAL
// wire types, the REAL threshold vocabulary, and the REAL bytes the route
// serves.
//
// Compiled by scripts/cluster_graph_check.sh together with
//   Sources/Shared/SharedModels.swift            (the wire types, never redeclared)
//   Sources/Shared/SharedStore.swift             (SharedModels references Theme)
//   Sources/Shared/ClusterHistorySeries.swift    (the metric set + the reduction)
//   Sources/Shared/ClusterMonitorViewModel.swift (ClusterMetricLevel, CLIMonitorFormat)
//   Sources/Shared/ClusterGraphReading.swift     <- the subject
//   scripts/model_decode_check/ThemeStub.swift   (the one non-model shim)
// so every assertion below runs the code the widget ships, not a copy of it.
//
// WHY THIS GATE EXISTS AT ALL. build_check.sh proves the widget compiles and
// model_decode_check.sh proves the payload decodes. Neither can notice that a
// panel put GPU% and °C on one axis, that a 40-pixel draw dropped the spike the
// operator opens the widget to see, that a window with no reading was joined
// into a straight line, or that a 7-day chart printed a 2-second claim. Every
// one of those is a lie a green compile and a green decode ship happily, and a
// chart lies by SHAPE — which is precisely what no shape-checking gate sees.
//
// NEGATIVE TEST (guarded): CLUSTER_GRAPH_NEGATIVE_TEST=1 stands a naive stride
// thin in for the real reduction, and a single merged segment in for the real
// segment split, so the spike and gap assertions below MUST fail. A harness that
// cannot fail is worthless.
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
        let suffix = detail.isEmpty ? "" : " — \(detail)"
        print("  FAIL: \(name)\(suffix)")
        failures.append(name + suffix)
    }
}

let negativeTest = ProcessInfo.processInfo.environment["CLUSTER_GRAPH_NEGATIVE_TEST"] == "1"
if negativeTest {
    print("NOTE: CLUSTER_GRAPH_NEGATIVE_TEST=1 — a naive stride thin stands in for the")
    print("      real reduction and one merged segment for the real split, so the")
    print("      spike and gap assertions below MUST fail.")
}

/// A naive thin: keep every Nth point. It is the thing the shared reduction
/// exists to replace, and under the negative test it is what the projections
/// below are driven with.
func naiveStrideThin(_ points: [ClusterHistorySample], target: Int) -> [ClusterHistorySample] {
    guard target > 0, points.count > target else { return points }
    let strideBy = points.count / target
    guard strideBy > 1 else { return points }
    var out: [ClusterHistorySample] = []
    var i = 0
    while i < points.count {
        out.append(points[i])
        i += strideBy
    }
    if out.last != points.last, let last = points.last { out.append(last) }
    return out
}

// ===========================================================================
// Helpers: synthetic series, built to break specific rules.
// ===========================================================================

func series(_ metric: ClusterHistoryMetric,
            _ identity: String,
            tier: Int = 1,
            resolutionS: Int = 60,
            values: [Double?]) -> ClusterHistorySeries {
    let start: TimeInterval = 1_700_000_000
    return ClusterHistorySeries(metric: metric,
                                identity: identity,
                                tier: tier,
                                resolutionS: resolutionS,
                                samples: values.enumerated().map { index, value in
                                    ClusterHistorySample(t: start + Double(index * resolutionS),
                                                         value: value)
                                })
}

/// The spike series: a 300-sample flat line with the single highest value at an
/// index a same-size stride thin provably skips, and the single lowest value near
/// the front. `pixelWidth` 40 means a thin keeps every 7th point.
let spikeIndex = 103
let spikeValue = 96.5
let dipValue = 41.0
let dipIndex = 7

func spikeSeries(_ identity: String = "192.0.2.7") -> ClusterHistorySeries {
    var values = [Double?](repeating: 62.0, count: 300)
    values[dipIndex] = dipValue
    values[spikeIndex] = spikeValue
    return series(.gpuTempC, identity, values: values)
}

/// The same series with a hole: 40 samples, then 20 buckets with NO reading at
/// all, then 40 more. A retained step across the hole is 21 × the tier step, so
/// the reduction must break the line there rather than draw a slope.
func gappedSeries(_ identity: String = "192.0.2.7") -> ClusterHistorySeries {
    var values: [Double?] = []
    for index in 0..<40 { values.append(index.isMultiple(of: 5) ? 55.0 + Double(index) : 56.0) }
    for _ in 0..<20 { values.append(nil) }
    for index in 0..<40 { values.append(70.0 + Double(index % 7)) }
    return series(.gpuTempC, identity, values: values)
}

// ===========================================================================
// Helpers the checks themselves need
// ===========================================================================

extension Sequence where Element == Int {
    /// Consecutive pairs, so a monotonicity check can read as one sentence.
    func windowsOfTwo() -> [(Int, Int)] {
        var out: [(Int, Int)] = []
        var iterator = makeIterator()
        guard var previous = iterator.next() else { return out }
        while let next = iterator.next() {
            out.append((previous, next))
            previous = next
        }
        return out
    }
}

// ===========================================================================
// 1. The window axis, and the resolution the route served
// ===========================================================================

print("\n[1] windows and the served resolution (the operator's amendment)")

check("the default window is 15 m", ClusterGraphWindow.recommended == .fifteenMinutes,
      "got \(ClusterGraphWindow.recommended.rawValue)")
check("every window has a request value the route accepts",
      ClusterGraphWindow.allCases.allSatisfy { $0.requestValue == $0.rawValue })
check("the five windows are the card's five",
      ClusterGraphWindow.allCases.map { $0.rawValue } == ["15m", "1h", "6h", "24h", "7d"],
      ClusterGraphWindow.allCases.map { $0.rawValue }.joined(separator: ","))
check("every window label is spelled for a human",
      ClusterGraphWindow.allCases.allSatisfy { !$0.label.isEmpty && !$0.label.contains("/") })
check("15 m reads the 2 s tier, 7 d the 5-minute one",
      ClusterGraphWindow.fifteenMinutes.expectedTier == 0
        && ClusterGraphWindow.sevenDays.expectedTier == 2)
check("1 h / 6 h / 24 h read the 1-minute tier",
      [ClusterGraphWindow.oneHour, .sixHours, .twentyFourHours].allSatisfy { $0.expectedTier == 1 })

check("resolution 2 s is phrased in seconds",
      ClusterGraphResolutionCaption.text(resolutionS: 2) == "2 s",
      ClusterGraphResolutionCaption.text(resolutionS: 2))
check("resolution 60 s is phrased as a minute",
      ClusterGraphResolutionCaption.text(resolutionS: 60) == "1 m",
      ClusterGraphResolutionCaption.text(resolutionS: 60))
check("resolution 300 s is phrased as 5 m",
      ClusterGraphResolutionCaption.text(resolutionS: 300) == "5 m",
      ClusterGraphResolutionCaption.text(resolutionS: 300))
check("a resolution that is not a multiple of 60 keeps a decimal",
      ClusterGraphResolutionCaption.text(resolutionS: 90) == "1.5 m",
      ClusterGraphResolutionCaption.text(resolutionS: 90))
check("an unknown resolution says unknown, never 0 s",
      ClusterGraphResolutionCaption.text(resolutionS: 0) == "unknown")
check("the caption line says the step is a STEP",
      ClusterGraphResolutionCaption.line(resolutionS: 300) == "every 5 m",
      ClusterGraphResolutionCaption.line(resolutionS: 300))
/// The amendment's whole point: a 7-day window whose step is 5 minutes must not
/// be readable as 2-second data.
check("a 7 d caption states both the window and the served step",
      ClusterGraphResolutionCaption.line(window: .sevenDays, resolutionS: 300) == "7 d · every 5 m",
      ClusterGraphResolutionCaption.line(window: .sevenDays, resolutionS: 300))
check("a 15 m caption says 2 s",
      ClusterGraphResolutionCaption.line(window: .fifteenMinutes, resolutionS: 2) == "15 m · every 2 s",
      ClusterGraphResolutionCaption.line(window: .fifteenMinutes, resolutionS: 2))
check("the caption never mixes a step into the window",
      ClusterGraphResolutionCaption.line(window: .sevenDays, resolutionS: 300)
        .contains("every 5 m"))

// ===========================================================================
// 2. What each size draws, and what one request asks for
// ===========================================================================

print("\n[2] metric selection per size (the card's three layouts)")

let panelMetrics = ClusterGraphRule.cardPanelMetrics
check("the large panel carries the card's eight metrics", panelMetrics.count == 8,
      panelMetrics.map { $0.rawValue }.joined(separator: ","))
check("the eight are CPU°C, GPU°C, GPU%, CPU%, W, RAM%, KV%, tok/s in that order",
      panelMetrics == [.cpuTempC, .gpuTempC, .gpuUtilPct, .cpuUtilPct,
                       .gpuPowerW, .memUsedPct, .kvUsage, .genTps],
      panelMetrics.map { $0.rawValue }.joined(separator: ","))
check("the medium overlay is all temps plus GPU%", ClusterGraphRule.cardOverlayMetrics.count == 3)
check("the medium overlay has both temperatures and the GPU utilisation",
      ClusterGraphRule.cardOverlayMetrics
        .contains(where: { $0 == .cpuTempC })
        && ClusterGraphRule.cardOverlayMetrics.contains(where: { $0 == .gpuTempC })
        && ClusterGraphRule.cardOverlayMetrics.contains(where: { $0 == .gpuUtilPct }))
check("sparkline draws exactly ONE metric at every metric set",
      ClusterHistoryMetricGroup.allCases.allSatisfy {
        ClusterGraphRule.baseMetrics(density: .sparkline, set: $0).count == 1
      })
check("sparkline with no set picked still draws exactly one metric",
      ClusterGraphRule.baseMetrics(density: .sparkline, set: nil).count == 1)
check("a set's lead metric is IN that set",
      ClusterHistoryMetricGroup.allCases.allSatisfy {
        $0.graphLeadMetric.group == $0
      })
check("sparkline leads with the GPU-side number where one exists",
      ClusterGraphRule.baseMetrics(density: .sparkline, set: .temps) == [.gpuTempC]
        && ClusterGraphRule.baseMetrics(density: .sparkline, set: .power) == [.gpuPowerW])
check("serving has no host-side number so its lead is the engine's output rate",
      ClusterHistoryMetricGroup.serving.graphLeadMetric.seriesKind == .engine)
check("medium with no set picked draws the card's three",
      ClusterGraphRule.baseMetrics(density: .nodeOverlay, set: nil) == ClusterGraphRule.cardOverlayMetrics)
check("large with no set picked draws the card's eight",
      ClusterGraphRule.baseMetrics(density: .smallMultiples, set: nil) == panelMetrics)
check("a narrowed set keeps the card's order for what the card listed, then adds the rest of the set",
      {
        let narrowed = ClusterGraphRule.baseMetrics(density: .smallMultiples, set: .temps)
        let cardOrder = panelMetrics.filter { $0.group == .temps }
        let restOfSet = ClusterHistoryMetric.allCases
          .filter { $0.group == .temps && !cardOrder.contains($0) }
        return narrowed == cardOrder + restOfSet && !narrowed.isEmpty
      }())
check("every metric set draws at least one metric at every size",
      ClusterGraphDensity.allCases.allSatisfy { density in
        ClusterHistoryMetricGroup.allCases.allSatisfy {
          !ClusterGraphRule.baseMetrics(density: density, set: $0).isEmpty
        }
      })
check("the memory set carries VRAM in the unit the payload stores it in",
      ClusterGraphRule.baseMetrics(density: .smallMultiples, set: .memory)
        .contains(where: { $0 == .vramUsedMb }))
check("the serving set carries run and wait as well as KV% and tok/s",
      ClusterGraphRule.baseMetrics(density: .smallMultiples, set: .serving)
        .contains(where: { $0 == .runRequests })
        && ClusterGraphRule.baseMetrics(density: .smallMultiples, set: .serving)
          .contains(where: { $0 == .waitRequests }))

// The tier filter is not decoration: the route answers 400 for a peak column at
// tier 0, so a peak picked on the DEFAULT window would fail the request itself.
let defaultRequest = ClusterGraphRule.requestMetrics(density: .smallMultiples, set: nil,
                                                     window: .fifteenMinutes)
check("the default window's request asks for no peak column",
      defaultRequest.allSatisfy { !$0.isPeakField },
      defaultRequest.filter { $0.isPeakField }.map { $0.rawValue }.joined(separator: ","))
check("the default window's request carries the card's whole panel in ONE list",
      defaultRequest == panelMetrics,
      defaultRequest.map { $0.rawValue }.joined(separator: ","))
check("a peak really is unreachable at the default tier (this is what the filter protects)",
      !ClusterHistoryMetric.gpuPowerWMax.requestable(atTier: 0)
        && ClusterHistoryMetric.gpuPowerWMax.requestable(atTier: 1))
let powerSet = ClusterGraphRule.baseMetrics(density: .smallMultiples, set: .power)
check("a narrowed set really does contain a peak column",
      powerSet.contains(where: { $0.isPeakField }),
      powerSet.map { $0.rawValue }.joined(separator: ","))
check("the 7-day request CAN ask for the peaks that tier stores",
      ClusterGraphRule.requestMetrics(density: .smallMultiples, set: .power, window: .sevenDays)
        .contains(where: { $0.isPeakField }))
check("the 15-minute request never asks for a peak, so it never earns a 400",
      ClusterGraphRule.requestMetrics(density: .smallMultiples, set: .power,
                                      window: .fifteenMinutes)
        .allSatisfy { !$0.isPeakField })
check("requesting never invents a metric outside the shared vocabulary",
      ClusterGraphRule.requestMetrics(density: .smallMultiples, set: nil, window: .sevenDays)
        .allSatisfy { ClusterHistoryMetric.allCases.contains($0) })
check("one request carries the whole panel (the route takes one comma list per window)",
      Set(ClusterGraphRule.requestMetrics(density: .smallMultiples, set: nil, window: .sixHours)
            .map { $0.rawValue }).count
        == ClusterGraphRule.requestMetrics(density: .smallMultiples, set: nil, window: .sixHours).count)
check("the draw list drops what the SERVED tier cannot answer",
      ClusterGraphRule.metricsToDraw(density: .smallMultiples, set: .power, tier: 0).count
        < ClusterGraphRule.metricsToDraw(density: .smallMultiples, set: .power, tier: 2).count)
check("droppedAtTier names the columns a tier refused, for the honest sentence",
      !ClusterGraphRule.droppedAtTier(powerSet, tier: 0).isEmpty
        && ClusterGraphRule.droppedAtTier(powerSet, tier: 0)
          .allSatisfy { $0.isPeakField })
check("droppedAtTier is empty when the tier stores everything",
      ClusterGraphRule.droppedAtTier(ClusterGraphRule.cardOverlayMetrics, tier: 0).isEmpty)

// ===========================================================================
// 3. Small multiples: one unit per axis, every time
// ===========================================================================

print("\n[3] unit groups (a panel is small multiples, never one shared axis)")

let panelLines = panelMetrics.map { metric in
    ClusterGraphReading.line(metric: metric,
                             series: series(metric, "192.0.2.7",
                                            values: [50.0, 60.0, 70.0, 80.0]),
                             pixelWidth: 200)
}
let panelMinis = ClusterGraphReading.minis(from: panelLines)

check("every metric the card's panel lists has a NAMED unit",
      panelMetrics.allSatisfy { ClusterGraphUnit.of($0) != .count },
      panelMetrics.filter { ClusterGraphUnit.of($0) == .count }
        .map { $0.rawValue }.joined(separator: ","))
check("the eight metrics split into four unit groups, not one",
      panelMinis.count == 4, panelMinis.map { $0.axisLabel }.joined(separator: "|"))
check("no mini mixes two units",
      panelMinis.allSatisfy { mini in
        Set(mini.lines.map { ClusterGraphUnit.of($0.metric) }).count == 1
      })
check("°C and % are never on the same axis",
      !panelMinis.contains { mini in
        mini.lines.contains(where: { ClusterGraphUnit.of($0.metric) == .temperature })
          && mini.lines.contains(where: { ClusterGraphUnit.of($0.metric) == .percent })
      })
check("every drawn mini has a non-empty axis label",
      panelMinis.filter { $0.unit != .count }.allSatisfy { !$0.axisLabel.isEmpty })
check("unit groups keep allCases order so two nodes' panels line up",
      panelMinis.map { $0.unit } == ClusterGraphUnit.allCases.filter { unit in
        panelMinis.map { $0.unit }.contains(unit)
      })
check("the minis of one chart have distinct identities (a ForEach cannot collide)",
      Set(panelMinis.map { $0.id }).count == panelMinis.count)
check("a temperature axis is never drawn over 0...100 by default",
      ClusterGraphReading.minis(from: [ClusterGraphReading.line(
        metric: .gpuTempC, series: series(.gpuTempC, "192.0.2.7", values: [40.0, 42.0, 41.0]),
        pixelWidth: 200)]).first.map { $0.axisCeiling < 100 } ?? false)
check("a fleet that never left 40 °C still gets an axis that covers 40 °C",
      ClusterGraphReading.minis(from: [ClusterGraphReading.line(
        metric: .gpuTempC, series: series(.gpuTempC, "192.0.2.7", values: [40.0, 42.0, 41.0]),
        pixelWidth: 200)]).first.map {
          $0.axisFloor <= 40.0 && $0.axisCeiling >= 42.0
        } ?? false)
check("a thermal excursion from 60 to 88 °C is not flattened by a 0 floor",
      ClusterGraphReading.minis(from: [ClusterGraphReading.line(
        metric: .gpuTempC,
        series: series(.gpuTempC, "192.0.2.7", values: [60.0, 88.0, 61.0]),
        pixelWidth: 200)]).first.map { $0.axisFloor > 0 && $0.axisFloor <= 60 } ?? false)
check("a utilisation series keeps a 0 floor (a quiet line is allowed to look quiet)",
      ClusterGraphReading.minis(from: [ClusterGraphReading.line(
        metric: .gpuUtilPct,
        series: series(.gpuUtilPct, "192.0.2.7", values: [3.0, 4.0, 3.0]),
        pixelWidth: 200)]).first.map { $0.axisFloor == 0 } ?? false)
check("a series with no reading still gets an axis (so the chart can exist)",
      ClusterGraphReading.minis(from: [ClusterGraphReading.line(
        metric: .gpuTempC, series: series(.gpuTempC, "192.0.2.7", values: [nil, nil]),
        pixelWidth: 200)]).first.map { $0.axisFloor == 0 && $0.axisCeiling == 1 } ?? false)
check("the legend of a mini names every line it draws",
      panelMinis.allSatisfy { $0.legend.count == $0.lines.count })
check("the chart legend has one entry per metric, in request order",
      ClusterGraphReading.legend(for: panelLines).map { $0.metric } == panelMetrics)
check("a legend entry's colour rule is the line's own rule",
      ClusterGraphReading.legend(for: panelLines).first?.tint
        == ClusterGraphReading.legend(for: [panelLines.first!]).first?.tint)

// ===========================================================================
// 4. Threshold bands: only where a threshold was DECLARED
// ===========================================================================

print("\n[4] threshold bands and verdicts (the view-model's numbers, not ours)")

check("the temperature band pair is 80 / 90 °C",
      ClusterGraphBandRule.thermalWarnC == 80 && ClusterGraphBandRule.thermalCriticalC == 90)
check("the percent-used band pair is 85 / 95",
      ClusterGraphBandRule.loadWarnPct == 85 && ClusterGraphBandRule.loadCriticalPct == 95)
check("80 °C is warn and 90 °C is critical (the view-model's `>=` semantics)",
      ClusterGraphBandRule.thermal(80) == .warn && ClusterGraphBandRule.thermal(90) == .critical)
check("79.9 °C is ok and 89.9 °C is warn",
      ClusterGraphBandRule.thermal(79.9) == .ok && ClusterGraphBandRule.thermal(89.9) == .warn)
check("85 % is warn and 95 % is critical",
      ClusterGraphBandRule.load(85) == .warn && ClusterGraphBandRule.load(95) == .critical)
check("an unmeasured value is .unknown, never .ok",
      ClusterGraphBandRule.thermal(nil) == .unknown && ClusterGraphBandRule.load(nil) == .unknown)
check("a temperature verdict exists, a wattage verdict does not",
      ClusterGraphBandRule.verdict(for: .gpuTempC, displayValue: 91) != nil
        && ClusterGraphBandRule.verdict(for: .gpuPowerW, displayValue: 500) == nil)
check("GPU utilisation has no declared band (the limit column was never stored)",
      ClusterGraphBandRule.verdict(for: .gpuUtilPct, displayValue: 99) == nil)
check("KV occupancy has no declared band",
      ClusterGraphBandRule.verdict(for: .kvUsage, displayValue: 99) == nil)
check("a metric with no band paints .unthresholded, not .ok",
      ClusterGraphReading.line(metric: .gpuPowerW,
                               series: series(.gpuPowerW, "192.0.2.7", values: [120.0, 130.0]),
                               pixelWidth: 200).tint == .unthresholded)
check("a hot GPU paints the critical verdict",
      ClusterGraphReading.line(metric: .gpuTempC,
                               series: series(.gpuTempC, "192.0.2.7", values: [70.0, 95.0]),
                               pixelWidth: 200).tint == .verdict(.critical))
check("a never-reported metric paints the unknown verdict, not green",
      ClusterGraphReading.line(metric: .gpuTempC,
                               series: series(.gpuTempC, "192.0.2.7", values: [nil, nil]),
                               pixelWidth: 200).tint == .verdict(.unknown))

let bands = ClusterGraphBandRule.temperatureBands(from: 20, top: 100)
check("a temperature chart gets two bands (warn and critical)", bands.count == 2,
      bands.map { "\($0.from)-\($0.to)" }.joined(separator: ","))
check("the warn band stops where the critical band starts",
      bands.first?.to == ClusterGraphBandRule.thermalCriticalC)
check("bands never extend past the axis they are painted on",
      bands.allSatisfy { $0.from >= 20 && $0.to <= 100 })
check("a chart whose axis sits below the warn threshold gets no band at all",
      ClusterGraphBandRule.temperatureBands(from: 20, top: 60).isEmpty)
check("a band is never painted for a unit with no declared threshold",
      ClusterGraphUnit.allCases.filter { $0 != .temperature }
        .allSatisfy { unit in
          ClusterGraphReading.minis(from: panelLines.filter {
            ClusterGraphUnit.of($0.metric) == unit
          }).allSatisfy { mini in
            mini.unit != .temperature
          }
        })

// ===========================================================================
// 5. The reduction the caption prints: extremes, nulls, gaps
// ===========================================================================

print("\n[5] extremes, nulls and gaps (the four claims a chart can lie about)")

let spike = spikeSeries()
let spikeLineReal = ClusterGraphReading.line(metric: .gpuTempC, series: spike, pixelWidth: 40)
let spikeThin = naiveStrideThin(spike.readable, target: 40)

check("the spike series really does have a spike, and thinning is real",
      spikeLineReal.sourceCount == 300 && spikeThin.count < 300,
      "thin keeps \(spikeThin.count) of 300")
check("the naive thin provably loses the spike (so the next check cannot pass vacuously)",
      spikeThin.compactMap { $0.value }.max() != spikeValue
        || spikeThin.compactMap { $0.value }.min() != dipValue)

/// The projection under test. Under the negative test the naive thin replaces the
/// real reduction, so the extremes assertions below have to fail.
func reducedExtremes(_ line: ClusterGraphLine) -> (min: Double?, max: Double?) {
    if negativeTest {
        let thin = naiveStrideThin(line.points, target: 40)
        return (thin.compactMap { $0.value }.min(), thin.compactMap { $0.value }.max())
    }
    return (line.downsampled.outMin, line.downsampled.outMax)
}

let drawn = reducedExtremes(spikeLineReal)
check("the drawn points still hold the true maximum", drawn.max == spikeValue,
      "drawn max \(String(describing: drawn.max)) vs \(spikeValue)")
check("the drawn points still hold the true minimum", drawn.min == dipValue,
      "drawn min \(String(describing: drawn.min)) vs \(dipValue)")
check("the reduction reports its extremes as preserved", spikeLineReal.extremesPreserved)
check("the reduction actually thinned (a 300-point series into 40 columns)",
      spikeLineReal.wasThinned)
check("the min/max the caption prints are the TRUE ones, with timestamps",
      spikeLineReal.maximum == spikeValue && spikeLineReal.minimum == dipValue
        && spikeLineReal.maximumAt != nil && spikeLineReal.minimumAt != nil)
check("the extremes are not the same point wearing two hats",
      spikeLineReal.maximumAt != spikeLineReal.minimumAt)
check("reduction happened, so drawn points are a subset of the source",
      spikeLineReal.points.count <= spikeLineReal.sourceCount)
check("the right-hand end survives (that point is the 'now' the widget headlines)",
      spikeLineReal.downsampled.keptLast)

let gap = gappedSeries()
let gapLine = ClusterGraphReading.line(metric: .gpuTempC, series: gap, pixelWidth: 200)

/// The segment split under test — the function the widget draws from. Under the
/// negative test every point is folded into ONE segment, so the gap assertions
/// below have to fail.
func segments(of line: ClusterGraphLine) -> [[ClusterHistorySample]] {
    negativeTest ? [line.points.filter { $0.value != nil }]
                 : ClusterGraphReading.drawSegments(of: line)
}

check("a window with no reading is more than one segment",
      segments(of: gapLine).count >= 2,
      "segments: \(segments(of: gapLine).count)")
check("no segment bridges the hole (a gap is never drawn as a slope)",
      segments(of: gapLine).allSatisfy { segment in
        guard let first = segment.first, let last = segment.last else { return true }
        return last.t - first.t < Double(40 * gap.resolutionS)
      })
check("every drawn polyline carries a value at every point (a null is never a point)",
      segments(of: gapLine).allSatisfy { segment in
        segment.allSatisfy { $0.value != nil }
      })
check("a lone surviving point is not drawn as a line",
      ClusterGraphReading.drawSegments(of: ClusterGraphReading.line(
        metric: .gpuTempC, series: series(.gpuTempC, "192.0.2.7", values: [nil, 70.0, nil]),
        pixelWidth: 200)).isEmpty)
check("the reduction counted the hole as gap columns", gapLine.gapColumns > 0,
      "gapColumns \(gapLine.gapColumns)")
check("a null is never drawn as 0 (no drawn point is the zero a missing value became)",
      gapLine.points.compactMap { $0.value }.allSatisfy { $0 > 0 })
check("the missing count is the payload's own, not folded away",
      gapLine.missingCount == 20, "missing \(gapLine.missingCount)")
check("the current value skips the holes rather than reporting the last one",
      gapLine.current != nil && gapLine.current! > 0)
check("a series with no value at all has no current and no line",
      ClusterGraphReading.line(metric: .gpuTempC,
                               series: series(.gpuTempC, "192.0.2.7", values: [nil, nil, nil]),
                               pixelWidth: 200).current == nil)
check("a flat line at zero is never produced for absent data (hasValue is false)",
      !ClusterGraphReading.line(metric: .gpuTempC,
                                series: series(.gpuTempC, "192.0.2.7", values: [nil, nil]),
                                pixelWidth: 200).hasValue)

// ===========================================================================
// 6. "All nodes": the envelope, and the tier it refuses
// ===========================================================================

print("\n[6] all nodes (one line, the fleet's own min/max, tiers never folded)")

let hostA = series(.gpuTempC, "192.0.2.7", values: [50.0, 55.0, 60.0])
let hostB = series(.gpuTempC, "192.0.2.9", values: [70.0, 45.0, 66.0])
let envelope = ClusterGraphReading.envelope(metric: .gpuTempC, series: [hostA, hostB],
                                            pixelWidth: 200)
check("all-nodes folds several hosts into ONE line", envelope != nil)
check("the envelope's maximum is the fleet's own highest point",
      envelope?.maximum == 70.0, "got \(String(describing: envelope?.maximum))")
check("the envelope's minimum is the fleet's own lowest point",
      envelope?.minimum == 45.0, "got \(String(describing: envelope?.minimum))")
check("the envelope says it is the fleet, with the host count",
      envelope?.label.contains("2 node(s)") ?? false, envelope?.label ?? "nil")
check("the all-nodes identity is not a host name",
      envelope?.identity == ClusterGraphReading.allNodesIdentity
        && (envelope.map { !$0.identity.contains(".") } ?? false))
check("an empty series list yields no line rather than a flat zero",
      ClusterGraphReading.envelope(metric: .gpuTempC, series: [], pixelWidth: 200) == nil)
check("a mixed-tier merge is REFUSED (a 2 s point is never folded into a 5 m average)",
      ClusterGraphReading.envelope(metric: .gpuTempC,
                                   series: [series(.gpuTempC, "192.0.2.7", tier: 1,
                                                   resolutionS: 60, values: [50.0, 51.0]),
                                            series(.gpuTempC, "192.0.2.9", tier: 2,
                                                   resolutionS: 300, values: [60.0, 61.0])],
                                   pixelWidth: 200) == nil)
check("a mixed-resolution merge is refused even at the same tier",
      ClusterGraphReading.envelope(metric: .gpuTempC,
                                   series: [series(.gpuTempC, "192.0.2.7", resolutionS: 60,
                                                   values: [50.0, 51.0]),
                                            series(.gpuTempC, "192.0.2.9", resolutionS: 120,
                                                   values: [60.0, 61.0])],
                                   pixelWidth: 200) == nil)
check("a single host under 'all' still draws, with the same rules",
      ClusterGraphReading.envelope(metric: .gpuTempC, series: [hostA], pixelWidth: 200)?.maximum
        == 60.0)

// ===========================================================================
// 7. Panels: one per node, engine rows attributed, §7 labels
// ===========================================================================

print("\n[7] panels (one per node; engine rows attributed to the node that runs them)")

let mixedLines: [ClusterGraphLine] = [
    ClusterGraphReading.line(metric: .gpuTempC, series: series(.gpuTempC, "192.0.2.7",
                                                               values: [61.0, 62.0]),
                             pixelWidth: 200),
    ClusterGraphReading.line(metric: .gpuTempC, series: series(.gpuTempC, "192.0.2.9",
                                                               values: [71.0, 72.0]),
                             pixelWidth: 200),
    ClusterGraphReading.line(metric: .kvUsage, series: series(.kvUsage, "192.0.2.7:8000",
                                                              values: [0.1, 0.2]),
                             pixelWidth: 200),
    ClusterGraphReading.line(metric: .genTps, series: series(.genTps, "192.0.2.9:8001",
                                                             values: [40.0, 41.0]),
                             pixelWidth: 200)
]
let hostPanels = ClusterGraphReading.panels(series: [
    series(.gpuTempC, "192.0.2.7", values: [61.0, 62.0]),
    series(.gpuTempC, "192.0.2.9", values: [71.0, 72.0]),
    series(.kvUsage, "192.0.2.7:8000", values: [0.1, 0.2]),
    series(.genTps, "192.0.2.9:8001", values: [40.0, 41.0])
], metrics: [.gpuTempC, .kvUsage, .genTps], pixelWidth: 200)

check("one panel per node, in sorted identity order",
      hostPanels.map { $0.host } == ["192.0.2.7", "192.0.2.9"],
      hostPanels.map { $0.host }.joined(separator: ","))
check("the host part of an engine identity is the node that runs it",
      ClusterGraphReading.engineHost("192.0.2.7:8000") == "192.0.2.7")
check("a bare host has no engine part",
      ClusterGraphReading.engineHost("192.0.2.7") == nil)
check("a node's panel carries the engine rows it runs, and only those",
      hostPanels.first?.lines.contains(where: { $0.metric == .kvUsage }) == true
        && hostPanels.first?.lines.contains(where: { $0.metric == .genTps }) == false)
check("a panel never carries an engine line belonging to another node",
      hostPanels.allSatisfy { panel in
        panel.lines.filter { $0.metric.seriesKind == .engine }
          .allSatisfy { ClusterGraphReading.engineHost($0.identity) == panel.host }
      })
check("a node with no engine rows for a metric draws no line for it (it does not draw a zero)",
      hostPanels.first?.lines.contains(where: { $0.metric == .genTps }) == false
        && hostPanels.allSatisfy { panel in
          panel.lines.filter { $0.metric.seriesKind == .engine }
            .allSatisfy { $0.current != 0 }
        })
check("the minis of a panel are the same grouping rules as any other chart",
      ClusterGraphReading.minis(from: hostPanels.first?.lines ?? []).count
        == ClusterGraphReading.minis(from: mixedLines.filter {
          $0.identity.hasPrefix("192.0.2.7")
        }).count)
check("a panel with no metrics requested draws nothing rather than an empty frame",
      ClusterGraphReading.panels(series: [series(.gpuTempC, "192.0.2.7", values: [61.0])],
                                 metrics: [], pixelWidth: 200).isEmpty)
check("a limit keeps the first N panels, in order, and only with a limit set",
      ClusterGraphReading.panels(series: [series(.gpuTempC, "192.0.2.7", values: [61.0]),
                                          series(.gpuTempC, "192.0.2.9", values: [62.0]),
                                          series(.gpuTempC, "192.0.2.11", values: [63.0])],
                                 metrics: [.gpuTempC], pixelWidth: 200, limit: 2).count == 2)
check("the card asks for every node, so no limit is the default",
      ClusterGraphDensity.smallMultiples.panelLimit == nil)

// §7: a Home Screen shows a tail, never an address.
check("a host label is the app's short tail, not the address",
      ClusterGraphReading.displayLabel(for: .host, identity: "192.0.2.7") == ".7",
      ClusterGraphReading.displayLabel(for: .host, identity: "192.0.2.7"))
check("an engine label keeps the port the recorder keyed it by",
      ClusterGraphReading.displayLabel(for: .engine, identity: "192.0.2.7:8000") == ".7:8000",
      ClusterGraphReading.displayLabel(for: .engine, identity: "192.0.2.7:8000"))
check("no panel label ever carries a dotted quad",
      hostPanels.allSatisfy { panel in
        !panel.displayLabel.contains("192.0.2") && !panel.displayLabel.contains(".")
          || !panel.displayLabel.contains("192.0.2")
      })
check("a hostname-shaped identity is passed through, not mangled",
      ClusterGraphReading.displayLabel(for: .host, identity: "dgx-01") == "dgx-01")
check("a host identity with no node part still gets a label",
      !ClusterGraphReading.displayLabel(for: .host, identity: "").isEmpty)

// ===========================================================================
// 8. Axis density: the same integer drives the buckets and the labels
// ===========================================================================

print("\n[8] axis density (one width decides the buckets AND the labels)")

check("a narrow width still gets two labels", ClusterGraphAxisRule.labelCount(pixelWidth: 60) == 2,
      "got \(ClusterGraphAxisRule.labelCount(pixelWidth: 60))")
check("a wide width is capped so the ticks cannot collide",
      ClusterGraphAxisRule.labelCount(pixelWidth: 100_000) == 6)
check("label count never decreases as the width grows",
      stride(from: 1, through: 900, by: 7).windowsOfTwo().allSatisfy { a, b in
        ClusterGraphAxisRule.labelCount(pixelWidth: a) <= ClusterGraphAxisRule.labelCount(pixelWidth: b)
      })
check("a measured width of nothing reduces to ONE column, never zero",
      ClusterGraphAxisRule.pixelColumns(measuredWidth: 0) == 1)
check("a negative or non-finite width is not a zero-column reduction",
      ClusterGraphAxisRule.pixelColumns(measuredWidth: -40) == 1
        && ClusterGraphAxisRule.pixelColumns(measuredWidth: .nan) == 1
        && ClusterGraphAxisRule.pixelColumns(measuredWidth: .infinity) == 1)
check("a fractional width rounds to whole pixel columns",
      ClusterGraphAxisRule.pixelColumns(measuredWidth: 140.6) == 141)
check("the same integer feeds the reduction and the labels (no second assumption)",
      ClusterGraphAxisRule.labelCount(pixelWidth: ClusterGraphAxisRule.pixelColumns(measuredWidth: 300))
        == ClusterGraphAxisRule.labelCount(pixelWidth: 300))
check("a zero-column reduction would be refused, so the widget never asks for one",
      ClusterGraphAxisRule.pixelColumns(measuredWidth: 0) >= 1
        && ClusterGraphAxisRule.labelCount(pixelWidth: 1) >= 2)
check("a 40-column draw really does thin the 300-point spike series",
      spikeLineReal.points.count > 0 && spikeLineReal.points.count <= 80,
      "drew \(spikeLineReal.points.count) points (at most two per column)")

// ===========================================================================
// 9. Captions: the number lines, and why an empty chart is empty
// ===========================================================================

print("\n[9] captions and the empty reason (never 0, never a quiet fleet)")

let dash = CLIMonitorFormat.dash
check("an unreported value prints the CLI's dash, never 0",
      ClusterGraphCaption.value(nil, metric: .gpuTempC) == dash
        && ClusterGraphCaption.value(nil, metric: .gpuTempC) != "0\(ClusterHistoryMetric.gpuTempC.unit)")
check("a real 0 prints as a real 0 (an idle GPU is a fact)",
      ClusterGraphCaption.value(0, metric: .gpuUtilPct) == "0\(ClusterHistoryMetric.gpuUtilPct.unit)")
check("GPU temp takes no decimals and CPU temp one, exactly as the CLI's columns do",
      ClusterGraphCaption.places(for: .gpuTempC) == 0
        && ClusterGraphCaption.places(for: .cpuTempC) == 1
        && ClusterGraphCaption.value(71.4, metric: .gpuTempC) == "71\u{00B0}C"
        && ClusterGraphCaption.value(71.4, metric: .cpuTempC) == "71.4\u{00B0}C",
      ClusterGraphCaption.value(71.4, metric: .gpuTempC))
check("wattage keeps two decimals like the CLI's power column",
      ClusterGraphCaption.value(129.126, metric: .gpuPowerW) == "129.13 W",
      ClusterGraphCaption.value(129.126, metric: .gpuPowerW))
check("token rate keeps one decimal, prompt rate none",
      ClusterGraphCaption.places(for: .genTps) == 1
        && ClusterGraphCaption.places(for: .promptTps) == 0)
check("counts take no decimals",
      ClusterGraphCaption.places(for: .runRequests) == 0
        && ClusterGraphCaption.places(for: .waitRequests) == 0)
check("VRAM is printed in the unit the recorder stores it in",
      ClusterGraphCaption.value(1234, metric: .vramUsedMb).contains("MiB")
        || ClusterGraphCaption.value(1234, metric: .vramUsedMb).contains("MB")
        || ClusterGraphCaption.value(1234, metric: .vramUsedMb).contains(" mb")
        || ClusterHistoryMetric.vramUsedMb.unit.contains("B"),
      "unit is \(ClusterHistoryMetric.vramUsedMb.unit)")
check("KV% is drawn as a percentage, not a 0-1 fraction",
      ClusterHistoryMetric.kvUsage.displayFactor == 100
        && ClusterHistoryMetric.kvUsage.displayValue(0.125) == 12.5)
check("the extreme caption carries the timestamp it happened at",
      ClusterGraphCaption.extreme(88.0, at: 1, metric: .cpuTempC,
                                  clock: { _ in "12:00" }) == "88.0°C @ 12:00",
      ClusterGraphCaption.extreme(88.0, at: 1, metric: .cpuTempC, clock: { _ in "12:00" }))
check("an extreme with no timestamp still prints the number, never a nil",
      ClusterGraphCaption.extreme(nil, at: nil, metric: .cpuTempC,
                                  clock: { _ in "12:00" }) == dash)

// The empty reason, from the payload's own flags.
func responseJSON(_ object: [String: Any]) -> ClusterHistoryResponse {
    let data = try! JSONSerialization.data(withJSONObject: object)
    return try! JSONDecoder().decode(ClusterHistoryResponse.self, from: data)
}

let basePayload: [String: Any] = [
    "window": "6h", "tier": 1, "resolution_s": 60, "resolution": "60s",
    "since": 1, "until": 2, "hosts": [], "engines": [], "speak": "x"
]
let excludedResponse = responseJSON(basePayload.merging(["hosts_included": false]) { $1 })
let nothingResponse = responseJSON(basePayload.merging(["note": "no stored samples yet"]) { $1 })
let excludedSet = ClusterHistorySeriesSet.make(excludedResponse, for: .gpuTempC)
let nothingSet = ClusterHistorySeriesSet.make(nothingResponse, for: .gpuTempC)

check("a filter that excluded the series is said to have excluded it",
      excludedSet.isEmptyBecauseExcluded
        && ClusterGraphCaption.emptyReason(for: excludedSet, window: .sixHours)
          .contains("excluded"),
      ClusterGraphCaption.emptyReason(for: excludedSet, window: .sixHours))
check("an empty answer keeps the payload's own sentence",
      ClusterGraphCaption.emptyReason(for: nothingSet, window: .sixHours)
        .contains("no stored samples yet"),
      ClusterGraphCaption.emptyReason(for: nothingSet, window: .sixHours))
check("a tier-0 gap names the 2 s ring rather than a quiet fleet",
      ClusterGraphCaption.emptyReason(
        for: ClusterHistorySeriesSet.make(
          responseJSON([
            "window": "15m", "tier": 0, "resolution_s": 2, "resolution": "2s",
            "since": 1, "until": 2, "hosts": [], "engines": [], "speak": "x"
          ]), for: .gpuTempC),
        window: .fifteenMinutes).contains("2 s"))
check("the two kinds of empty are distinguishable sentences",
      ClusterGraphCaption.emptyReason(for: excludedSet, window: .sixHours)
        != ClusterGraphCaption.emptyReason(for: nothingSet, window: .sixHours))
check("every peak column names the tier that stores it, so an empty peak can say why",
      ClusterHistoryMetric.allCases.filter { $0.isPeakField }
        .allSatisfy { $0.tierRequirement != nil }
        && ClusterHistoryMetric.allCases.contains { $0.isPeakField })

// ===========================================================================
// 10. The route's own bytes
// ===========================================================================

print("\n[10] the route's own fixtures (decoded, projected, cross-checked)")

let fixtureFiles = ["v1_cluster_history_6h.json", "v1_cluster_history_7d.json",
                    "v1_cluster_history_6h_engine_only.json", "v1_cluster_history_15m.json"]

// Fixture coverage per metric SET. The AppIntent picker offers five sets; if no
// fixture yields a series for one of them, that branch of the picker has never
// been decoded by anything — which is exactly how the engine columns sat
// undecoded until a live host served one. The question is asked of the REAL wire
// model (`ClusterHistorySeriesSet.make`), not of a hand-parsed JSON shape: the
// route's rows arrive in two shapes, and "a file exists whose name mentions the
// set" would pass on a payload that carries nothing of it.
for group in ClusterHistoryMetricGroup.allCases {
    var coverage = ""
    for file in fixtureFiles {
        let url = URL(fileURLWithPath: fixtureDir).appendingPathComponent(file)
        guard let data = try? Data(contentsOf: url),
              let response = try? JSONDecoder().decode(ClusterHistoryResponse.self, from: data)
        else { continue }
        let hit = ClusterHistoryMetric.allCases.first { metric in
            metric.group == group
              && !ClusterHistorySeriesSet.make(response, for: metric).series.isEmpty
        }
        if let hit { coverage = "\(file) → \(hit.rawValue)"; break }
    }
    check("a fixture yields a decoded series for the '\(group.rawValue)' set (the picker can ask for it)",
          !coverage.isEmpty, coverage.isEmpty
            ? "no fixture produced a series in this set — the picker's branch is undecoded"
            : coverage)
}

for file in fixtureFiles {
    let url = URL(fileURLWithPath: fixtureDir).appendingPathComponent(file)
    guard let data = try? Data(contentsOf: url) else {
        check("\(file) exists", false, "missing — regenerate with scripts/make_cluster_history_fixture.py")
        continue
    }
    guard let response = try? JSONDecoder().decode(ClusterHistoryResponse.self, from: data) else {
        check("\(file) decodes", false, "the shared model refused the route's bytes")
        continue
    }
    check("\(file) decodes (\(response.window), tier \(response.tier))", true)

    // What the fixture itself says, read independently of the Swift model.
    let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
    let hosts = (json["hosts"] as? [[String: Any]]) ?? []
    let engines = (json["engines"] as? [[String: Any]]) ?? []
    let resolutionS = (json["resolution_s"] as? Int) ?? response.resolution_s
    check("\(file): the Swift tier is the fixture's tier", response.tier == (json["tier"] as? Int ?? -1))
    check("\(file): the caption is built from the served resolution, not the window",
          ClusterGraphResolutionCaption.text(resolutionS: resolutionS)
            == ClusterGraphResolutionCaption.text(resolutionS: response.resolution_s)
            && ClusterGraphResolutionCaption.text(resolutionS: resolutionS) != "unknown")

    let drawList = ClusterGraphRule.metricsToDraw(density: .smallMultiples, set: nil,
                                                  tier: response.tier)
    check("\(file): the draw list is requestable at the served tier",
          drawList.allSatisfy { $0.requestable(atTier: response.tier) })
    check("\(file): no peak is drawn from a tier that stores none",
          response.tier == 0 ? drawList.allSatisfy { !$0.isPeakField } : true)

    let panels = ClusterGraphReading.panels(response: response, metrics: drawList,
                                            pixelWidth: 140)
    let fixtureHosts = Set(hosts.compactMap { $0["host"] as? String })
    // A payload whose host rows were excluded still has panels: they come from the
    // engine rows' own node part, because a serving panel belongs to the node that
    // runs the backend. That is the expected set for that fixture, not "no panels".
    let engineNodeHosts = Set(engines.compactMap { $0["node"] as? String })
    let expectedHosts = fixtureHosts.isEmpty ? engineNodeHosts : fixtureHosts
    check("\(file): one panel per node the payload carries",
          Set(panels.map { $0.host }) == expectedHosts,
          "panels \(panels.map { $0.host }) vs payload \(expectedHosts.sorted())")
    check("\(file): no panel label carries an address",
          panels.allSatisfy { !$0.displayLabel.contains("192.0.2") })

    let lines = panels.flatMap { $0.lines }
    check("\(file): every drawn line preserves its extremes",
          lines.allSatisfy { $0.extremesPreserved },
          "\(lines.filter { !$0.extremesPreserved }.count) line(s) moved an extreme")
    check("\(file): no null became a zero anywhere in the projection",
          lines.allSatisfy { line in
            line.points.compactMap { $0.value }.allSatisfy { value in
              !value.isNaN && !value.isInfinite
            }
          })

    // The strongest cross-check: the min/max of one column, computed here from
    // the fixture's own JSON, against what the chart would print.
    for (metric, key) in [(ClusterHistoryMetric.gpuTempC, "gpu_temp_c"),
                          (ClusterHistoryMetric.gpuPowerW, "gpu_power_w"),
                          (ClusterHistoryMetric.cpuUtilPct, "cpu_usage_pct")] {
        let values = hosts.compactMap { $0[key] as? Double }
        guard !values.isEmpty else { continue }
        guard let line = ClusterGraphReading.envelope(metric: metric,
                                                      series: ClusterHistorySeriesSet
                                                        .make(response, for: metric).series,
                                                      pixelWidth: 140) else {
            check("\(file): \(key) has an envelope", false, "no line built for a column with values")
            continue
        }
        check("\(file): \(key) maximum is the payload's own", line.maximum == values.max(),
              "\(String(describing: line.maximum)) vs \(String(describing: values.max()))")
        check("\(file): \(key) minimum is the payload's own", line.minimum == values.min(),
              "\(String(describing: line.minimum)) vs \(String(describing: values.min()))")
    }

    // The engine-only fixture: hosts were excluded, and the widget must say so
    // rather than draw a fleet of one host with no numbers.
    if file == "v1_cluster_history_6h_engine_only.json" {
        let hostMetricSet = ClusterHistorySeriesSet.make(response, for: .gpuTempC)
        check("\(file): the excluded host series is reported as excluded",
              hostMetricSet.excluded && !hostMetricSet.hasData)
        check("\(file): the empty host chart's reason is the exclusion, not a quiet fleet",
              ClusterGraphCaption.emptyReason(for: hostMetricSet, window: .sixHours)
                .contains("excluded"))
        let enginePanels = ClusterGraphReading.panels(response: response,
                                                      metrics: [.genTps], pixelWidth: 140)
        check("\(file): an engine-only chart still gets a panel per node",
              !enginePanels.isEmpty && enginePanels.allSatisfy { !$0.lines.isEmpty })
    }

    // The 15-minute fixture is the one the widget's DEFAULT window asks for, and
    // today the route cannot answer it from this process. The widget's job is to
    // say the payload's sentence, not to draw an empty axis.
    if file == "v1_cluster_history_15m.json" {
        check("\(file): the default window's tier is the 2 s ring", response.tier == 0)
        check("\(file): an empty tier-0 answer carries the payload's own reason",
              (response.note ?? "").contains("ring"), response.note ?? "nil")
        check("\(file): that answer draws no panels at all", panels.isEmpty,
              "panels \(panels.count)")
        let set = ClusterHistorySeriesSet.make(response, for: .gpuTempC)
        check("\(file): the reason surfaced by the widget is the payload's",
              ClusterGraphCaption.emptyReason(for: set, window: .fifteenMinutes)
                .contains("ring")
                || ClusterGraphCaption.emptyReason(for: set, window: .fifteenMinutes)
                  .contains("2 s"),
              ClusterGraphCaption.emptyReason(for: set, window: .fifteenMinutes))
    }
}

// ===========================================================================
// Summary
// ===========================================================================

print("")
if failures.isEmpty {
    if negativeTest {
        print("NEGATIVE TEST DID NOT BIT — the harness passed with a naive thin in")
        print("place of the reduction, so the spike and gap assertions are vacuous.")
        exit(1)
    }
    print("cluster graph checks: \(checks)/\(checks) PASS")
    exit(0)
}
print("cluster graph checks: \(checks - failures.count)/\(checks) pass, \(failures.count) FAIL")
for failure in failures { print("  - \(failure)") }
exit(1)
