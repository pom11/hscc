import Foundation
import WidgetKit

// ===========================================================================
// The cluster widget's data + text layer.
//
// Split out of `ClusterWidget.swift` (the provider) and `ClusterWidgetViews.swift`
// (the layout) on purpose. WidgetKit marks every `accessory*` family
// `@available(macOS, unavailable)`, so the view file that must name them cannot
// be compiled on the host at all — and everything a reviewer needs EXECUTED (the
// projection from the two reads, the nil-is-not-zero honesty rules, the heat
// bands, the text rules, which families pay for the board row) is exactly that
// layer. It lives here, compiles for macOS, and
// `scripts/cluster_widget_check.sh` runs it against the committed
// `/v1/cluster/monitor` fixtures. The two files it came from keep the WidgetKit
// wiring and the SwiftUI layout.
// ===========================================================================

// ---------------------------------------------------------------------------
// Cluster widget — the projection, the entry and the text rules.
//
// The provider and the WidgetKit configuration live in `ClusterWidget.swift`,
// the layout in `ClusterWidgetViews.swift` (the same file set): five families
// from one entry — systemSmall, systemMedium and the three Lock Screen
// accessories. This layer owns how a fetched payload becomes the handful of
// numbers a widget is allowed to draw (WIDGET_DATA_CONTRACT §2's budget, §5.5's
// field list, §7's redaction), and it is deliberately free of view types so
// `scripts/cluster_widget_check.sh` can execute it.
//
// Reads (t_56b910c8): dataset 5 is served by two routes, and this surface uses
// exactly those two per refresh — §2's "≤ 2 requests per surface per refresh":
//
//   * `GET /v1/autodown/status` — the fleet power state (`up` / `waking` /
//     `down`), which is the headline, plus the idle minutes before autodown
//     fires.
//   * `GET /v1/cluster/monitor` — every host the monitor tried to sample, with
//     `error` / `sample` / `workloads`, which answers nodes up/down, models
//     served and GPU / temperature / load in the SAME body.
//
// `GET /v1/cluster/status` is deliberately NOT read here any more. §4.4
// measured it answering 39 B `{"speak": "cluster status unavailable"}` when the
// fleet is powered off, and `ClusterStatusResponse` declares `workloads` /
// `total_hosts` as REQUIRED — so on a down fleet that read threw a decode
// failure, the provider's `guard let auto, let cluster` failed, and the widget
// rendered "Can't reach the cluster" for a cluster the API had just told us was
// DOWN. Everything that read contributed (a model count, a host count) is
// carried by `/v1/cluster/monitor`, which degrades to a decodable
// `{"speak": ...}` instead (§4.4, same measurement).
//
// The board work row (running / queue / blocked) is dataset 3, not this
// dataset, and it stays — but it is now CONDITIONAL: the accessory families
// render no board row (`WidgetFamily.showsBoardWork`) and the operator can turn
// the section off in Settings, so those three reads are skipped entirely. An
// accessory refresh is therefore 2 requests, and the board row's own budget is
// the kanban trio §5.3 sanctions.
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// The projection: /v1/cluster/monitor -> the numbers this surface draws.
// ---------------------------------------------------------------------------

/// What the cluster monitor says about the FLEET, projected to seven numbers
/// (t_56b910c8). This is the only place the monitor payload is interpreted, so
/// the "which aggregate and why" decisions are in one readable place and are
/// checkable headless by `scripts/cluster_widget_check.sh`.
///
/// Every field is nil-able and nil MEANS "this body did not report it" — never
/// zero. §6 rule 1's discipline (a missing key is a bug, zero is a value)
/// applies to a projection too: rendering an unmeasured metric as `0` tells the
/// operator the GPU is idle when the truth is that the node was not sampled.
struct ClusterFleet: Equatable {
    /// Hosts the monitor reported with no `error` — they answered.
    let nodesUp: Int?
    /// Hosts the monitor listed at all (errored ones included: the route lists
    /// what it tried to sample, so this is the denominator, not a guess).
    let nodesTotal: Int?
    /// DISTINCT `cluster_id`s across the hosts. A tensor-parallel model holds
    /// both hosts of a pair under one id, so summing per-host workload counts
    /// would double-count it (4 hosts / 2 models in the committed fixture).
    let modelsServed: Int?
    /// Busiest GPU utilisation across the hosts, in percent.
    let gpuUtilPct: Double?
    /// Hottest GPU, in °C.
    let gpuTempC: Double?
    /// Hottest CPU, in °C.
    let cpuTempC: Double?
    /// Highest 1-minute load average across the hosts.
    let load1m: Double?

    /// Project the wire payload.
    ///
    /// nil when the route DEGRADED (`json` absent — §4.4's 38 B down-state
    /// answer): "no data" must stay distinguishable from "four hosts, none up".
    init?(projecting response: MonitorResponse) {
        guard let payload = response.json else { return nil }
        // The host tally is not derived here any more. `ClusterNodeTally` is the
        // ONE projection of "hosts the route listed / hosts that answered", and
        // the cluster-health Live Activity runs the same call (t_f2867fb3). Two
        // hand-written loops over `hosts` is how one surface ends up calling a
        // fleet degraded while the other calls it healthy, so there is now one
        // loop, and this widget's nil-on-no-host-list rule is that file's rule.
        let tally = ClusterNodeTally.project(response)
        self.nodesTotal = tally.nodesTotal
        self.nodesUp = tally.nodesUp
        let hosts = payload.hosts
        guard !hosts.isEmpty else {
            self.modelsServed = nil
            self.gpuUtilPct = nil
            self.gpuTempC = nil
            self.cpuTempC = nil
            self.load1m = nil
            return
        }
        let ids = Set(hosts.flatMap { $0.workloads ?? [] }.compactMap { $0.cluster_id })
        self.modelsServed = ids.count
        // MAX, not mean: one saturated node is the fact an operator acts on, and
        // an average of four hosts hides it (a 100% GPU on a 4-node fleet reads
        // 25% mean). Each metric is nil when NO host reported it.
        self.gpuUtilPct = Self.highest(hosts.compactMap { $0.sample?.gpuUtilPct })
        self.gpuTempC = Self.highest(hosts.compactMap { $0.sample?.gpuTempC })
        self.cpuTempC = Self.highest(hosts.compactMap { $0.sample?.cpuTempC })
        self.load1m = Self.highest(hosts.compactMap { $0.sample?.cpuLoad1m })
    }

    /// The maximum of the reported values, nil when nothing was reported.
    private static func highest(_ values: [Double]) -> Double? {
        values.max()
    }

    /// Whether this projection carries anything a metric line could draw.
    var hasMetrics: Bool {
        gpuUtilPct != nil || gpuTempC != nil || cpuTempC != nil || load1m != nil
    }
}

/// How hot a percentage or temperature reads, in the app's OWN display bands.
///
/// NOT a hardware spec: no route serves a thermal limit for these boxes, so the
/// widget never claims one — it colours, and it always prints the number with
/// its unit next to the colour. The bands are set above every value measured in
/// the committed 4-node fixture (63 °C GPU, 70.7 °C CPU, GPU util 0-88%) so the
/// normal state of a working fleet reads calm, and the utilisation bands match
/// `MonitorMeter.color(for:isCPU:)` exactly (CPU >85 warn / >95 bad, other
/// gauges >90 / >95) — a number and the monitor widget's bar for the same
/// metric can never disagree about what "hot" means.
enum ClusterMetricHeat: Equatable {
    case calm
    case warm
    case hot
    /// Nothing reported — render an em dash, never a colour.
    case unknown

    static func percent(_ value: Double?, isCPU: Bool = false) -> ClusterMetricHeat {
        guard let value else { return .unknown }
        let warn = isCPU ? 85.0 : 90.0
        if value > 95 { return .hot }
        if value > warn { return .warm }
        return .calm
    }

    static func celsius(_ value: Double?) -> ClusterMetricHeat {
        guard let value else { return .unknown }
        if value > 90 { return .hot }
        if value > 80 { return .warm }
        return .calm
    }

    /// Load has NO band on purpose: a load average is only hot relative to the
    /// core count, and no route serves the per-node core count (only the
    /// daemon-host sidecar carries `cpu.count`). Any band here would be an
    /// invented number, so load is printed uncoloured.
    static var load: ClusterMetricHeat { .unknown }
}

// ---------------------------------------------------------------------------
// Cluster widget data model.
// ---------------------------------------------------------------------------

/// A single widget snapshot. `.unreachable` carries the last-known state and
/// its age so the widget is never blank or stale-looking-live. `.unconfigured`
/// is the honest state when the operator hasn't set host/port/token yet.
///
/// The board work signals (`runningCards`, `queueDepth`, `blockedCards`) are
/// the "what the fleet is DOING" half: cards being worked, cards waiting in
/// the queue, and blocked cards (the failure indicator). Each is optional and
/// independently nil-able — a failed kanban fetch nils only that field, never
/// the whole widget (the state/topology live unreachable stay intact). A FAILED
/// read nils its count; it is never rendered as `0`.
///
/// The cluster signals added in t_56b910c8 (`nodesUp` / `nodesTotal` /
/// `gpuUtilPct` / `gpuTempC` / `cpuTempC` / `load1m`) follow the same rule and
/// are LIVE-ONLY: `SnapshotStore` does not persist a metric projection, so a
/// failed monitor read omits the metric line rather than showing a stale one
/// next to a live state line. `modelCount` does have a last-known value, so it
/// survives with `lastKnownAgeMinutes`.
struct ClusterEntry: TimelineEntry {
    let date: Date
    let state: ClusterState
    let pairs: [TopologyPair]
    let modelCount: Int?
    let idleMinutesRemaining: Int?
    /// Set when ANY part of this tile is last-known rather than live — the age
    /// of that data. Its presence is also the view's signal to dim the
    /// topology, so a stale strip is never mistaken for a live one.
    let lastKnownAgeMinutes: Int?
    /// Set only when `state == .unconfigured`.
    let configured: Bool
    /// Cards being worked right now (GET /v1/kanban/running count).
    let runningCards: Int?
    /// Cards sitting in the queue waiting to be picked up (ready/todo).
    let queueDepth: Int?
    /// Blocked cards needing attention — the failure indicator.
    let blockedCards: Int?
    /// True when the operator paused this surface in Settings (t_dea36c48).
    /// The widget still exists (a widget can't be removed programmatically) but
    /// renders an honest "paused in Settings" state instead of live data.
    let paused: Bool
    /// Hosts the monitor reported as up, nil when the monitor gave no host list.
    let nodesUp: Int?
    /// Hosts the monitor listed (errored included) — the denominator of
    /// `nodesUp`, nil when unknown. Never 0 unless the route really listed none.
    let nodesTotal: Int?
    /// Busiest GPU utilisation (percent), hottest GPU and CPU (°C), highest
    /// 1-minute load — all nil when the monitor did not report them.
    let gpuUtilPct: Double?
    let gpuTempC: Double?
    let cpuTempC: Double?
    let load1m: Double?
    /// The read that removed this tile's live data, when one did. The accessory
    /// families render `failure.shortLabel`; the system families render
    /// `.operatorLine`. nil while the tile is live.
    let failure: WidgetDataFailure?
    /// The content sections this surface renders (from Shared settings). A
    /// section the operator disabled is simply not drawn.
    let showSections: Set<String>
    /// True when the topology strip is the last-known one from the store rather
    /// than derived from this refresh. The strip is normally a STATE glyph, so
    /// a live state with a stale model count must not dim it — dimming means
    /// "these dots are old data", which is only true when they came from storage.
    let pairsAreStale: Bool

    /// Explicit init rather than the synthesized memberwise one, so the
    /// pre-existing factories (`unconfigured`, `paused`) — which predate
    /// t_56b910c8 — keep compiling: the new signals default HERE. They cannot
    /// default at the declaration, because a `let` with a declaration default is
    /// already initialized and cannot be assigned from an initializer.
    init(date: Date,
         state: ClusterState,
         pairs: [TopologyPair],
         modelCount: Int?,
         idleMinutesRemaining: Int?,
         lastKnownAgeMinutes: Int?,
         configured: Bool,
         runningCards: Int?,
         queueDepth: Int?,
         blockedCards: Int?,
         paused: Bool,
         nodesUp: Int? = nil,
         nodesTotal: Int? = nil,
         gpuUtilPct: Double? = nil,
         gpuTempC: Double? = nil,
         cpuTempC: Double? = nil,
         load1m: Double? = nil,
         failure: WidgetDataFailure? = nil,
         showSections: Set<String> = WidgetSurfaceKind.widgetCluster.availableSections,
         pairsAreStale: Bool = false) {
        self.date = date
        self.state = state
        self.pairs = pairs
        self.modelCount = modelCount
        self.idleMinutesRemaining = idleMinutesRemaining
        self.lastKnownAgeMinutes = lastKnownAgeMinutes
        self.configured = configured
        self.runningCards = runningCards
        self.queueDepth = queueDepth
        self.blockedCards = blockedCards
        self.paused = paused
        self.nodesUp = nodesUp
        self.nodesTotal = nodesTotal
        self.gpuUtilPct = gpuUtilPct
        self.gpuTempC = gpuTempC
        self.cpuTempC = cpuTempC
        self.load1m = load1m
        self.failure = failure
        self.showSections = showSections
        self.pairsAreStale = pairsAreStale
    }

    /// The sections this surface knows about (drives fallback defaults).
    static let availableSections = WidgetSurfaceKind.widgetCluster.availableSections

    /// True when something here is last-known rather than live.
    var isStale: Bool { lastKnownAgeMinutes != nil }

    /// The one short string an accessory renders when it has NOTHING live to
    /// draw. The accessory views try real data first (the node gauge, the
    /// models count, the metric line) and fall back here, so a partly-degraded
    /// tile keeps showing what it does know.
    var shortGateLabel: String {
        if !configured { return "Set up in HSCC" }
        if paused { return "Paused" }
        return failure?.shortLabel ?? "No data"
    }

    static let unconfigured = ClusterEntry(date: .now,
                                           state: .unknown,
                                           pairs: [],
                                           modelCount: nil,
                                           idleMinutesRemaining: nil,
                                           lastKnownAgeMinutes: nil,
                                           configured: false,
                                           runningCards: nil,
                                           queueDepth: nil,
                                           blockedCards: nil,
                                           paused: false)

    static func paused(_ date: Date = .now) -> ClusterEntry {
        ClusterEntry(date: date,
                     state: .unknown,
                     pairs: [],
                     modelCount: nil,
                     idleMinutesRemaining: nil,
                     lastKnownAgeMinutes: nil,
                     configured: true,
                     runningCards: nil,
                     queueDepth: nil,
                     blockedCards: nil,
                     paused: true)
    }
}

/// The board work row's three counts (dataset 3). All optional, independently:
/// a kanban read that fails nils its own count and never zeroes another's.
struct BoardWork: Equatable {
    let running: Int?
    let queueDepth: Int?
    let blocked: Int?
}

// ---------------------------------------------------------------------------
// Which families render the board row.
// ---------------------------------------------------------------------------

extension WidgetFamily {
    /// True for the families that draw the board work row, false for the four
    /// Lock Screen accessories — they have no room for it (one line, one gauge,
    /// two lines), so the provider must not spend three kanban reads on them
    /// (§2's budget). Written as the positive list of system families because
    /// WidgetKit marks the `accessory*` cases `@available(macOS, unavailable)`,
    /// and this decision has to be executable in the gate. `systemExtraLarge`
    /// and anything newer keep the row: a new size losing a row is the worse
    /// error than a Lock Screen complication gaining one.
    var showsBoardWork: Bool {
        switch self {
        case .systemSmall, .systemMedium, .systemLarge, .systemExtraLarge:
            return true
        default:
            return false
        }
    }
}

// ---------------------------------------------------------------------------
// Text + colour rules (executed by the gate, rendered here)
// ---------------------------------------------------------------------------

/// How each entry field reads as text. Every function returns an em dash for
/// nil: an unmeasured metric is absent, never `0` (§6 rule 1's discipline).
enum ClusterMetricText {
    static let dash = "—"

    /// "88%" — a utilisation percent, rounded.
    static func percent(_ value: Double?) -> String {
        guard let value else { return dash }
        return "\(Int(value.rounded()))%"
    }

    /// "63°C" — a temperature, rounded. The unit rides with the number so the
    /// colour can never be the only thing carrying meaning.
    static func celsius(_ value: Double?) -> String {
        guard let value else { return dash }
        return "\(Int(value.rounded()))°C"
    }

    /// "2.7" — a load average, one decimal. Uncoloured by design: no route
    /// serves the per-node core count (`ClusterMetricHeat.load`), so a band
    /// would be an invented threshold.
    static func load(_ value: Double?) -> String {
        guard let value else { return dash }
        return String(format: "%.1f", value)
    }

    /// "3/4" — hosts up over hosts listed. nil either side means the monitor
    /// did not report, which renders as the dash, not as "0/0".
    static func nodes(up: Int?, total: Int?) -> String {
        guard let up, let total else { return dash }
        return "\(up)/\(total)"
    }

    /// "2 models" / "1 model" — the count only. Workload names, `recipe` paths
    /// and container names are never decoded on this path (§7), so there is
    /// nothing here that could render one.
    static func models(_ count: Int?) -> String {
        guard let count else { return dash }
        return count == 1 ? "1 model" : "\(count) models"
    }

    /// The metric strip's tokens in scan order — the GPU, then how hot it is,
    /// then the CPU, then load — each paired with the heat band that colours it.
    /// A nil metric drops its WHOLE token rather than printing a dash next to
    /// the others: on a line this short, three dashes are noise, and an
    /// unmeasured metric is absent, not zero.
    static func tokens(gpuUtilPct: Double?, gpuTempC: Double?,
                       cpuTempC: Double?, load1m: Double?) -> [(label: String, heat: ClusterMetricHeat)] {
        var out: [(String, ClusterMetricHeat)] = []
        if let gpuUtilPct {
            out.append(("GPU " + percent(gpuUtilPct), ClusterMetricHeat.percent(gpuUtilPct)))
        }
        if let gpuTempC {
            out.append(("GPU " + celsius(gpuTempC), ClusterMetricHeat.celsius(gpuTempC)))
        }
        if let cpuTempC {
            out.append(("CPU " + celsius(cpuTempC), ClusterMetricHeat.celsius(cpuTempC)))
        }
        if let load1m {
            out.append(("load " + load(load1m), ClusterMetricHeat.load))
        }
        return out
    }

    /// The strip as plain strings — `tokens()` without the colours, which is
    /// what a single-line family joins with " · ".
    static func metricParts(gpuUtilPct: Double?, gpuTempC: Double?, cpuTempC: Double?, load1m: Double?) -> [String] {
        tokens(gpuUtilPct: gpuUtilPct, gpuTempC: gpuTempC, cpuTempC: cpuTempC, load1m: load1m).map { $0.label }
    }

    /// The `accessoryInline` line: nodes up, models served, and the GPU — the
    /// three facts that survive one line of Lock Screen real estate. Falls back
    /// to the gate label when the entry carries nothing live.
    static func inlineLine(nodesUp: Int?, nodesTotal: Int?, models: Int?,
                           gpuUtilPct: Double?, gateLabel: String) -> String {
        var parts: [String] = []
        let nodeText = nodes(up: nodesUp, total: nodesTotal)
        if nodeText != dash { parts.append(nodeText + " up") }
        // Qualified: the parameter `models` shadows this type's own static func
        // inside the body (a plain `models(served)` reads as calling an Int?).
        if let served = models { parts.append(ClusterMetricText.models(served)) }
        if let gpuUtilPct { parts.append("GPU " + percent(gpuUtilPct)) }
        return parts.isEmpty ? gateLabel : parts.joined(separator: " · ")
    }
}
