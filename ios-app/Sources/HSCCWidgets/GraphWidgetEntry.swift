import Foundation
import WidgetKit

// ---------------------------------------------------------------------------
// The graph widget's entry: what one refresh knows, and the projection from a
// `GET /v1/cluster/history` answer into the lines each size draws.
//
// WHY THIS FILE EXISTS SEPARATELY. `ClusterGraphReading.swift` (Shared,
// Foundation-only) owns every rule about WHICH lines exist and HOW they are
// reduced, and `cluster_graph_check.sh` executes those rules on macOS. This file
// is the glue between that layer and WidgetKit: the entry, the copy for the two
// gate states, and the last-known series a widget falls back to when a refresh
// fails. Nothing here decides what a number means; it decides which refresh's
// numbers, and what to say when there are none.
//
// ONE HISTORY READ, ONE ROUTE. Every fetch behind this entry goes through
// `WidgetDataClient.clusterHistory`, which is `GET /v1/cluster/history` — the
// canonical metrics-history read (architect ruling DECISION-t_447cc7fc;
// `/v1/metrics/history` is retired). Nothing here names the retired path, and
// nothing here borrows `MetricsHistoryTrend`'s request shape.
// ---------------------------------------------------------------------------

// MARK: - Status

/// What went wrong with the surface, resolved by `WidgetData.read` — the same
/// states the other widget surfaces distinguish, because "the data is old but
/// real" and "there is no data" are different sentences to an operator.
///
/// `paused` and `unconfigured` are never produced by `WidgetData.read`: the
/// provider returns those entries before a client exists, so no read can have
/// happened behind them.
enum ClusterGraphStatus: Equatable {
    case live
    /// Real series, fetched `minutes` ago, kept because this refresh failed in a
    /// way honest staleness covers.
    case stale(minutes: Int, reason: WidgetDataFailure)
    case failed(WidgetDataFailure)
    /// The operator paused THIS surface in Settings. Neither live data, nor stale
    /// data, nor an error.
    case paused
    /// This device has no cluster address or token, so no read was attempted.
    case unconfigured

    var failure: WidgetDataFailure? {
        switch self {
        case .live, .paused, .unconfigured: return nil
        case .stale(_, let reason): return reason
        case .failed(let failure): return failure
        }
    }

    var ageMinutes: Int? {
        switch self {
        case .stale(let minutes, _): return minutes
        case .live, .paused, .unconfigured, .failed: return nil
        }
    }
}

// MARK: - The payload this widget keeps

/// The part of one `GET /v1/cluster/history` answer the widget needs, in a shape
/// it is allowed to keep in the App Group.
///
/// Declared here rather than reusing `ClusterHistoryResponse` because that model
/// is decode-side and §7 keeps server-side paths and raw prose out of shared
/// storage: this carries identities, tiers, resolutions, samples and the
/// payload's own one-line reason — and nothing else. Because the projection into
/// it happens through `ClusterHistorySeriesSet` (the Shared layer's own grouping),
/// the cache cannot disagree with the live path about which rows are which metric.
struct ClusterGraphPayload: Codable, Equatable {
    let series: [Series]
    /// The payload's `note`/`error`, kept verbatim because an empty chart's reason
    /// must be the server's sentence, not one this file invents.
    let note: String?

    struct Series: Codable, Equatable {
        let metric: String
        let identity: String
        let tier: Int
        let resolutionS: Int
        /// The payload said this metric's series for this kind was filtered out by
        /// the `metrics=` request — which is a different sentence from "the fleet
        /// had nothing to serve", and the empty state must not confuse them.
        let excluded: Bool
        let samples: [Sample]

        struct Sample: Codable, Equatable {
            let t: TimeInterval
            let v: Double?
        }
    }

    /// The response → the keepable shape. One place, so §7 is auditable in one
    /// line: identities, numbers, the resolution, the reason.
    static func project(from response: ClusterHistoryResponse,
                        metrics: [ClusterHistoryMetric]) -> ClusterGraphPayload {
        var out: [Series] = []
        for metric in metrics {
            let set = ClusterHistorySeriesSet.make(response, for: metric)
            for line in set.series {
                out.append(Series(metric: metric.rawValue,
                                  identity: line.identity,
                                  tier: line.tier,
                                  resolutionS: line.resolutionS,
                                  excluded: set.excluded,
                                  samples: line.samples.map { Series.Sample(t: $0.t, v: $0.value) }))
            }
        }
        return ClusterGraphPayload(series: out, note: response.note ?? response.error)
    }
}

// MARK: - Last-known payload

/// The last payload that came back, with the date it came back, so a failed
/// refresh can still draw a chart — with its age on it.
///
/// Bounded twice over: a sample ceiling, and a refusal to overwrite a good
/// snapshot with one that would exceed it. This is a widget value in the App
/// Group, not a database, and a half-stored history is worse than an older
/// complete one.
enum ClusterGraphSnapshotStore {
    /// The ceiling on total stored samples across every series in one snapshot. A
    /// 7-day window is 2 016 buckets per series, so eight metrics over four nodes
    /// is ~64 500 samples; past this the widget skips the save and keeps what it
    /// already has.
    static let sampleCeiling = 80_000

    private static let storageKey = "hscc.clusterGraph.payload"

    /// One payload per (window, node), so two graph configurations keep their
    /// own fallback instead of overwriting the single old slot (audit W12).
    private static func storageKey(window: String, node: String) -> String {
        storageKey + "." + window + "." + node
    }

    /// The node identities this device last saw, for the configuration sheet.
    ///
    /// Separate from the payload because the sheet needs a node list even when the
    /// history is empty (a fresh install has nodes and no stored series), and
    /// bounded because it is a `UserDefaults` value: the fleet measured four hosts,
    /// and a list that could grow without limit is not a widget value.
    static let hostLimit = 32
    private static let hostsKey = "hscc.clusterGraph.hosts"

    static func saveHosts(_ hosts: [String], limit: Int = hostLimit) {
        guard let data = try? JSONEncoder().encode(Array(hosts.prefix(limit))) else { return }
        UserDefaults(suiteName: AppGroup.suiteName)?.set(data, forKey: hostsKey)
    }

    static func loadHosts(limit: Int = hostLimit) -> [String] {
        guard let data = UserDefaults(suiteName: AppGroup.suiteName)?.data(forKey: hostsKey),
              let hosts = try? JSONDecoder().decode([String].self, from: data) else { return [] }
        return Array(hosts.prefix(limit))
    }

    struct Snapshot: Codable {
        let date: Date
        let window: String
        let node: String
        let payload: ClusterGraphPayload
    }

    /// Save a good read. False means the payload is over the ceiling and NOTHING
    /// was written — the previous snapshot is still there, whole.
    @discardableResult
    static func save(_ payload: ClusterGraphPayload, window: String, node: String,
                     at date: Date = Date()) -> Bool {
        let total = payload.series.reduce(0) { $0 + $1.samples.count }
        guard total <= sampleCeiling else { return false }
        guard let data = try? JSONEncoder().encode(Snapshot(date: date, window: window,
                                                            node: node, payload: payload)) else {
            return false
        }
        UserDefaults(suiteName: AppGroup.suiteName)?.set(data, forKey: storageKey(window: window,
                                                                                 node: node))
        return true
    }

    static func load(window: String, node: String) -> Snapshot? {
        guard let data = UserDefaults(suiteName: AppGroup.suiteName)?
            .data(forKey: storageKey(window: window, node: node)),
              let snapshot = try? JSONDecoder().decode(Snapshot.self, from: data) else { return nil }
        return snapshot
    }

    /// `WidgetData.read`'s `lastKnown` shape. A snapshot saved for a DIFFERENT
    /// window or node is refused here rather than drawn: a 15-minute chart built
    /// from a 7-day answer would carry an x axis describing a window the operator
    /// did not pick.
    static func lastKnown(window: String, node: String) -> (value: ClusterGraphPayload, at: Date)? {
        guard let snapshot = load(window: window, node: node),
              snapshot.window == window, snapshot.node == node else {
            return nil
        }
        return (value: snapshot.payload, at: snapshot.date)
    }
}

// MARK: - The entry

/// One refresh of the graph widget.
///
/// The entry carries the ANSWER and the CHOICES and derives the layout inputs. The
/// pixel width is deliberately NOT a stored field: it is measured at render time
/// (`ClusterGraphCanvas`) and passed into every projection call, because a width
/// recorded when the timeline was built is not the width the chart is drawn at, and
/// a reduction asked for the wrong width is the exact lie this widget's parent
/// layer exists to prevent.
struct ClusterGraphEntry: TimelineEntry {
    let date: Date
    let family: WidgetFamily
    /// The operator's configuration, already folded (nil = no node picked, nil =
    /// no metric set picked). Views never see the picker sentinels.
    let node: String?
    let set: ClusterHistoryMetricGroup?
    let window: ClusterGraphWindow
    /// The metrics this refresh asks for and draws, filtered to what the served
    /// tier can answer.
    let metrics: [ClusterHistoryMetric]
    /// The live payload (fetched now) or the last-known one (kept). nil means there
    /// is nothing to draw.
    let payload: ClusterGraphPayload?
    let status: ClusterGraphStatus

    // MARK: Gate

    var configured: Bool { status != .unconfigured }
    var paused: Bool { status == .paused }

    /// The single line the two gate states draw, in every family. Audit-3's rule
    /// is that an unconfigured app must never read as "paused" and a paused
    /// surface must never read as a failure. It holds here by construction rather
    /// than by branch order: `status` carries ONE of these states, so `paused` and
    /// `!configured` cannot both be true and the order below cannot produce the
    /// sentence the rule forbids. The provider keeps the same property at the other
    /// end — it decides the setup invite before the pause, which is what stops a
    /// device that lost its token while the surface was switched off from printing
    /// "Paused in Settings" forever. `cluster_graph_check.sh` measures that order
    /// inside the provider's own entry function.
    var gateLine: String {
        if paused { return "Paused in Settings" }
        return "Set up the app to see cluster history"
    }

    // MARK: Captions

    /// What this chart is: scope · window · set. A node's identity goes through
    /// `MonitorNode.shortTail` — the app's one rule for turning a host value into a
    /// label — so a Home Screen tile never prints an address.
    var scopeLine: String {
        let scope = node.map { MonitorNode.shortTail($0) } ?? "all nodes"
        return scope + " · " + window.label + " · " + (set?.graphLabel ?? "all metrics")
    }

    /// The resolution the ROUTE served for this answer — never a number this widget
    /// derives from its own window. `resolutionS` is nil only when no payload was
    /// ever decoded, and then there is no chart to disambiguate.
    var resolutionLine: String {
        guard let resolutionS, resolutionS > 0 else { return "" }
        return ClusterGraphResolutionCaption.line(window: window, resolutionS: resolutionS)
    }

    /// The footer: what the data is, and how old. A stale chart always carries its
    /// age, because a 15-minute window whose series is three hours old must say so
    /// out loud rather than look like a quiet fleet.
    var footerLine: String {
        if let age = status.ageMinutes {
            return "updated \(age)m ago · " + (status.failure?.shortLabel ?? "no answer")
        }
        return resolutionLine
    }

    var ageLine: String? {
        guard let age = status.ageMinutes else { return nil }
        return "updated \(age)m ago"
    }

    /// Why there is nothing to draw, in the payload's own words when it has any.
    /// Never "0", never a flat line, never "the fleet is quiet" for a filter that
    /// excluded the rows.
    var emptyLine: String? {
        if let note = payload?.note, !note.isEmpty { return note }
        if let failure = status.failure { return failure.shortLabel }
        if let first = metrics.first, let set = storedSet(for: first) {
            return ClusterGraphCaption.emptyReason(for: set, window: window)
        }
        if let resolutionS, resolutionS > 0 {
            return "no samples in the \(window.label) window at "
                + ClusterGraphResolutionCaption.text(resolutionS: resolutionS)
        }
        return "no stored samples"
    }

    /// The resolution the route served, read off the series themselves (the payload
    /// states it once and every series carries it, so an answer cannot disagree
    /// with its own rows).
    var resolutionS: Int? {
        payload?.series.first?.resolutionS
    }

    // MARK: Projection

    /// The one line a small widget draws: the set's lead metric, reduced to the
    /// width the view measured. nil means the caller renders `emptyLine`.
    func sparkline(pixelWidth: Int) -> ClusterGraphLine? {
        guard let metric = metrics.first else { return nil }
        let candidates = seriesFor(metric)
        guard !candidates.isEmpty else { return nil }
        if node == nil {
            return ClusterGraphReading.envelope(metric: metric, series: candidates,
                                                pixelWidth: pixelWidth)
        }
        guard let own = candidates.first(where: { $0.identity == node }) else { return nil }
        return ClusterGraphReading.line(metric: metric, series: own, pixelWidth: pixelWidth)
    }

    /// The mini-charts a medium widget draws: one per unit group over this node's
    /// (or the fleet's) lines, so °C and % never share an axis.
    func minis(pixelWidth: Int) -> [ClusterGraphMini] {
        ClusterGraphReading.minis(from: lines(pixelWidth: pixelWidth))
    }

    /// The panels a large / extra-large widget draws: one per node.
    func panels(pixelWidth: Int) -> [ClusterGraphPanel] {
        var drawn = ClusterGraphReading.panels(series: allSeries, metrics: metrics,
                                               pixelWidth: pixelWidth,
                                               limit: ClusterGraphDensity.smallMultiples.panelLimit)
        if let node {
            drawn = drawn.filter { $0.host == node }
        }
        return drawn
    }

    /// The mini a single line belongs to, so the small size draws its sparkline on
    /// the same domain rules the other sizes use instead of inventing its own.
    func mini(for line: ClusterGraphLine) -> ClusterGraphMini {
        ClusterGraphReading.minis(from: [line]).first
            ?? ClusterGraphMini(unit: ClusterGraphUnit.of(line.metric),
                                axisLabel: ClusterGraphUnit.of(line.metric).axisLabel,
                                lines: [line], axisFloor: 0, axisCeiling: 1)
    }

    /// Every line this refresh can draw, in metric order.
    func lines(pixelWidth: Int) -> [ClusterGraphLine] {
        var out: [ClusterGraphLine] = []
        for metric in metrics {
            let candidates = seriesFor(metric)
            guard !candidates.isEmpty else { continue }
            if node == nil {
                if let envelope = ClusterGraphReading.envelope(metric: metric,
                                                               series: candidates,
                                                               pixelWidth: pixelWidth) {
                    out.append(envelope)
                }
            } else if let own = candidates.first(where: { $0.identity == node }) {
                out.append(ClusterGraphReading.line(metric: metric, series: own,
                                                    pixelWidth: pixelWidth))
            }
        }
        return out
    }

    // MARK: - Series plumbing

    private var allSeries: [ClusterHistorySeries] {
        (payload?.series ?? []).map(Self.project)
    }

    /// The stored rows for one metric, re-projected into the Shared layer's series
    /// type so the reduction and the extremes are the SAME code the live path uses.
    private func seriesFor(_ metric: ClusterHistoryMetric) -> [ClusterHistorySeries] {
        (payload?.series ?? []).filter { $0.metric == metric.rawValue }.map(Self.project)
    }

    private static func project(_ stored: ClusterGraphPayload.Series) -> ClusterHistorySeries {
        ClusterHistorySeries(metric: ClusterHistoryMetric(rawValue: stored.metric) ?? .gpuTempC,
                             identity: stored.identity,
                             tier: stored.tier,
                             resolutionS: stored.resolutionS,
                             samples: stored.samples.map {
                                ClusterHistorySample(t: $0.t, value: $0.v)
                             })
    }

    /// The Shared layer's view of one metric's stored rows, for the honest
    /// empty-reason sentence (`ClusterHistorySeriesSet` is what separates "the
    /// filter excluded this series" from "the fleet had nothing").
    private func storedSet(for metric: ClusterHistoryMetric) -> ClusterHistorySeriesSet? {
        let candidates = seriesFor(metric)
        guard let first = payload?.series.first(where: { $0.metric == metric.rawValue }) else {
            return nil
        }
        return ClusterHistorySeriesSet(metric: metric,
                                       kind: metric.seriesKind,
                                       tier: first.tier,
                                       resolutionS: first.resolutionS,
                                       excluded: first.excluded,
                                       reason: payload?.note,
                                       series: candidates)
    }

    // MARK: - Placeholder

    /// The gallery/placeholder entry: an unconfigured device, which is also the
    /// honest preview — a chart of data this widget has never seen would be the §6
    /// lie in miniature.
    static let placeholder = ClusterGraphEntry(date: Date(),
                                               family: .systemMedium,
                                               node: nil,
                                               set: nil,
                                               window: ClusterGraphWindow.recommended,
                                               metrics: [],
                                               payload: nil,
                                               status: .unconfigured)
}
