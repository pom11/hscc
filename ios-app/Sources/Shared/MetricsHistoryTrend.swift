//
// MetricsHistoryTrend.swift — turn a `GET /v1/metrics/history` response into
// chart points, with the two rules the route's contract depends on (t_5ae8cfcf).
//
// 1. A `nil` metric stays `nil`. It never becomes 0.0 on the way into a series,
//    because "this node did not report a GPU temperature" and "the GPU is at
//    0 °C" are different facts, and a line that draws the first as the second
//    shows an idle node where there is a dead one. A gap is therefore a real
//    thing a surface can count (`gaps`) and label, not a flattening.
// 2. An average is never drawn alone for a field that has a peak. `gpu_temp_c`
//    is the mean of the bucket; `gpu_temp_c_max` is what it reached, and the
//    peak column exists precisely because a 1-minute mean erases the throttle
//    spike. `MetricsTrend.peakSeries` is non-nil whenever the tier carried
//    peaks, so a surface that draws only the average is making an observable
//    mistake — the type says whether the second line is available.
//
// The field lookup below is a `switch` over the decoded properties rather than
// a string-keyed lookup: a typo becomes a compile error instead of a chart that
// silently plots nothing.
//

import Foundation

/// A field a node series can be drawn for, keyed by the server's own name.
enum MetricsTrendField: String, CaseIterable {
    case cpuUsage = "cpu_usage_pct"
    case cpuTemp = "cpu_temp_c"
    case gpuUtil = "gpu_util_pct"
    case gpuTemp = "gpu_temp_c"
    case gpuPower = "gpu_power_w"
    case memUsed = "mem_used_pct"
    case gpuMem = "gpu_mem_used_mb"
    case gpuClock = "gpu_clock_mhz"

    /// The peak column stored beside this average, if the recorder keeps one.
    /// Only temps and power have peaks — the rest are honest as an average.
    var hasPeak: Bool {
        switch self {
        case .cpuTemp, .gpuTemp, .gpuPower: return true
        default: return false
        }
    }

    /// The unit suffix for a label. Deliberately not a format string: a widget
    /// renders these into a small, medium or large family.
    var unit: String {
        switch self {
        case .cpuUsage, .gpuUtil, .memUsed: return "%"
        case .cpuTemp, .gpuTemp: return "°C"
        case .gpuPower: return " W"
        case .gpuMem: return " MB"
        case .gpuClock: return " MHz"
        }
    }

    var label: String {
        switch self {
        case .cpuUsage: return "CPU"
        case .cpuTemp: return "CPU temp"
        case .gpuUtil: return "GPU util"
        case .gpuTemp: return "GPU temp"
        case .gpuPower: return "GPU power"
        case .memUsed: return "Memory"
        case .gpuMem: return "GPU memory"
        case .gpuClock: return "GPU clock"
        }
    }

    /// The average for this field out of one row. `nil` means "not reported".
    func average(of row: MetricsHostRow) -> Double? {
        switch self {
        case .cpuUsage: return row.cpu_usage_pct
        case .cpuTemp: return row.cpu_temp_c
        case .gpuUtil: return row.gpu_util_pct
        case .gpuTemp: return row.gpu_temp_c
        case .gpuPower: return row.gpu_power_w
        case .memUsed: return row.mem_used_pct
        case .gpuMem: return row.gpu_mem_used_mb
        case .gpuClock: return row.gpu_clock_mhz
        }
    }

    /// The peak for this field, or nil — nil on tier 0 (a single sample has no
    /// peak to report) and on any field the recorder does not peak.
    func peak(of row: MetricsHostRow) -> Double? {
        switch self {
        case .cpuTemp: return row.cpu_temp_c_max
        case .gpuTemp: return row.gpu_temp_c_max
        case .gpuPower: return row.gpu_power_w_max
        default: return nil
        }
    }
}

/// One bucket of one series. `value == nil` is a gap, and is kept that way.
struct MetricsTrendPoint: Equatable {
    let ts: Int
    let value: Double?
    let peak: Double?
    /// Samples that went into the bucket; nil on a row that did not report it.
    let samples: Int?

    var isGap: Bool { value == nil }
}

/// A chart-ready series for one host and one field, oldest bucket first.
struct MetricsTrend: Equatable {
    let field: MetricsTrendField
    let host: String
    let tier: Int
    /// Seconds per bucket (2 / 60 / 300), straight from `tier_meta`.
    let resolutionSeconds: Int
    let points: [MetricsTrendPoint]
    /// Whether THIS response carried peak columns for the field — false on
    /// tier 0 and on a field that has no peak. A surface must not invent one.
    let hasPeaks: Bool
    /// What the route said when it could not give a live tier-0 window, if that
    /// is the case here. Rendering this instead of an empty chart is the whole
    /// point of `tier0.readable_here`.
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
    var peakPoint: MetricsTrendPoint? {
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

    /// Build a series from a response, keeping one host and one field.
    ///
    /// Rows arrive ordered by `ts`; the order is preserved rather than sorted
    /// again, because the store's ordering is the one the widget's x-axis must
    /// match. `host` falls back to the response's own `host` filter, then to the
    /// first host present — a single-host chart is the only thing a widget
    /// family can draw honestly.
    static func make(_ response: MetricsHistoryResponse,
                     field: MetricsTrendField,
                     host: String? = nil) -> MetricsTrend {
        let wanted = host ?? response.host ?? response.hosts.first?.host ?? ""
        let rows = response.hosts.filter { ($0.host ?? "") == wanted }
        let points = rows.compactMap { row -> MetricsTrendPoint? in
            guard let ts = row.ts else { return nil }
            return MetricsTrendPoint(ts: ts, value: field.average(of: row),
                                     peak: field.peak(of: row), samples: row.n)
        }
        let carriedPeaks = field.hasPeak && rows.contains { field.peak(of: $0) != nil }
        var notice: String? = nil
        if response.tier == 0, response.tier0?.readable_here != true {
            notice = response.tier0?.reason
                ?? "the 2-second ring lives in the daemon process"
        }
        return MetricsTrend(field: field, host: wanted, tier: response.tier,
                            resolutionSeconds: response.tier_meta?.resolution_s ?? 0,
                            points: points, hasPeaks: carriedPeaks,
                            tier0Notice: notice)
    }
}
