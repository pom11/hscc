//
// ClusterHistoryTrend.swift — turn one `GET /v1/cluster/history` answer into
// chart points for one host and one field, with the two rules the history
// contract depends on (t_5ae8cfcf, re-pointed at the canonical route by
// t_0c99b017 — those rules are the contract, not the endpoint).
//
// 1. A `nil` metric stays `nil`. It never becomes 0.0 on the way into a series,
//    because "this node did not report a GPU temperature" and "the GPU is at
//    0 °C" are different facts, and a line that draws the first as the second
//    shows an idle node where there is a dead one. A gap is therefore a real
//    thing a surface can count (`gaps`) and label, not a flattening.
// 2. An average is never drawn alone for a field that has a peak. `gpu_temp_c`
//    is the mean of the bucket; `gpu_temp_c_max` is what it reached, and the
//    peak column exists precisely because a 1-minute mean erases the throttle
//    spike. `ClusterHistoryTrend.hasPeaks` says whether the second line is
//    available in THIS answer, so a surface that draws only the average where a
//    peak exists is making an observable mistake the type refuses to hide.
//
// The field type here IS `ClusterHistoryMetric` (`ClusterHistorySeries.swift`),
// not a trend-local vocabulary: one canonical route gets ONE metric vocabulary,
// and a second enum naming the same columns is exactly the drift class this file
// used to be — it carried `gpu_clock_mhz`, a column the canonical decoder does
// not even declare. The lookup is still a `switch` over the decoded properties
// rather than a string-keyed lookup, so a typo becomes a compile error instead
// of a chart that silently plots nothing.
//
// ONE PATH: this type reads only what `WidgetDataClient.clusterHistory` returns
// from `/v1/cluster/history`. It holds no URL, no path string and no second
// decoder — `ios-app/scripts/history_route_guard_check.sh` fails if any Swift
// source under `ios-app/Sources` or `macos-app/Sources` ever asks for a
// metrics-history path other than the canonical one.
//

import Foundation

extension ClusterHistoryMetric {
    /// The peak column stored beside this average, when the recorder keeps one.
    /// Only temps and power have peaks — the rest are honest as an average.
    /// `nil` for a field the recorder does not peak, and for a peak column
    /// itself (a peak has no peak of its own). Whether THIS answer carried the
    /// column at all is a per-response fact — tier 0 stores none, which is why
    /// `max_fields` exists and why `hasPeaks` is measured and never assumed.
    var peakCompanion: ClusterHistoryMetric? {
        switch self {
        case .cpuTempC: return .cpuTempCMax
        case .gpuTempC: return .gpuTempCMax
        case .gpuPowerW: return .gpuPowerWMax
        default: return nil
        }
    }

    /// The peak for this field out of one row, or nil — nil on tier 0 (a single
    /// sample has no peak to report) and on any field the recorder does not peak.
    func peak(in point: ClusterHistoryPoint) -> Double? {
        guard let companion = peakCompanion else { return nil }
        return companion.value(in: point)
    }
}

/// One bucket of one series. `value == nil` is a gap, and is kept that way.
struct ClusterHistoryTrendPoint: Equatable {
    let ts: Int
    let value: Double?
    let peak: Double?
    /// Samples that went into the bucket; nil on a row that did not report it.
    let samples: Int?

    var isGap: Bool { value == nil }
}

/// A chart-ready series for one host and one field, oldest bucket first.
struct ClusterHistoryTrend: Equatable {
    let field: ClusterHistoryMetric
    let host: String
    let tier: Int
    /// Seconds per bucket (2 / 60 / 300), read off the response — never derived
    /// from the spacing of the points, which a gap would make a lie.
    let resolutionSeconds: Int
    let points: [ClusterHistoryTrendPoint]
    /// Whether THIS response carried peak columns for the field — false on
    /// tier 0 and on a field that has no peak. A surface must not invent one.
    let hasPeaks: Bool
    /// What the route said when it could not give a live tier-0 window, if that
    /// is the case here. Rendering this instead of an empty chart is the whole
    /// point of `store.tier0_resident`: the 2 s ring is the daemon's memory, so
    /// an empty tier-0 answer is a process boundary, not an idle fleet.
    let tier0Notice: String?

    /// Buckets with no value. The honest caption for a chart that looks sparse.
    var gaps: Int { points.filter { $0.isGap }.count }

    /// A value to scale the y-axis by: the highest point INCLUDING the peaks.
    /// Scaling a temperature chart by the averages alone clips the spike the
    /// peak column exists for, which is the mistake this whole path avoids.
    var upperBound: Double? {
        let values = points.flatMap { [$0.value, $0.peak] }.compactMap { $0 }
        return values.max()
    }

    var lowerBound: Double? {
        let values = points.flatMap { [$0.value, $0.peak] }.compactMap { $0 }
        return values.min()
    }

    /// The peak value in the window, and when it happened (bucket start).
    var peakPoint: ClusterHistoryTrendPoint? {
        points.filter { $0.peak != nil }
            .max { ($0.peak ?? 0) < ($1.peak ?? 0) }
    }

    /// The series' own verdict for a caption line — never "0" for a gap.
    var statusLine: String {
        let res = resolutionSeconds
        let resText = res < 60 ? "\(res) s" : (res % 60 == 0 ? "\(res / 60) min" : "\(res) s")
        var line = "\(field.label) · tier \(tier) · \(resText) buckets"
        if !host.isEmpty {
            line += " · \(host)"
        }
        if let peakPoint, let peak = peakPoint.peak {
            line += " · peak \(Self.number(peak))\(field.unit)"
        }
        if gaps > 0 {
            line += " · \(gaps) not reported"
        }
        if points.isEmpty {
            line += " · no rows in this window"
        }
        if let notice = tier0Notice {
            line += " · \(notice)"
        }
        return line
    }

    /// A number without a trailing ".0" — the widget's other lines are plain.
    static func number(_ value: Double) -> String {
        if value == value.rounded() && abs(value) < 1e15 {
            return String(Int(value))
        }
        return String(format: "%.1f", value)
    }

    /// Build a series from a canonical history response, keeping one host and
    /// one field.
    ///
    /// Rows arrive ordered by `ts`; the order is preserved rather than sorted
    /// again, because the store's ordering is the one the widget's x-axis must
    /// match. `host` falls back to the response's own `node` filter, then to the
    /// first host present — a single-host chart is the only thing a widget
    /// family can draw honestly. The `node` filter is skipped as a fallback when
    /// it reads `all`, because that is the route's "every machine" spelling and
    /// matching it against a row's `host` would hand back an empty series that
    /// looks exactly like an empty fleet.
    static func make(_ response: ClusterHistoryResponse,
                     field: ClusterHistoryMetric,
                     host: String? = nil) -> ClusterHistoryTrend {
        var wanted = host ?? ""
        if wanted.isEmpty {
            if let filter = response.node, filter != "all", !filter.isEmpty {
                wanted = filter
            } else {
                wanted = response.hosts.first?.host ?? ""
            }
        }
        let rows = response.hosts.filter { ($0.host ?? "") == wanted }
        let points = rows.map {
            ClusterHistoryTrendPoint(ts: $0.ts, value: field.value(in: $0),
                                     peak: field.peak(in: $0), samples: $0.n)
        }
        let carriedPeaks = field.peakCompanion != nil
            && rows.contains { field.peak(in: $0) != nil }
        var notice: String? = nil
        if response.tier == 0, response.tier0Resident != true {
            let stated = (response.note ?? "")
                .trimmingCharacters(in: .whitespacesAndNewlines)
            notice = stated.isEmpty
                ? "the 2-second ring lives in the daemon process"
                : stated
        }
        return ClusterHistoryTrend(field: field, host: wanted, tier: response.tier,
                                   resolutionSeconds: response.resolution_s,
                                   points: points, hasPeaks: carriedPeaks,
                                   tier0Notice: notice)
    }
}
