import Foundation
import WidgetKit

// ===========================================================================
// The cluster widget's data + text layer.
//
// The contract is `hscc-api/docs/WIDGET_ROUTES.md` and the CLI behind it:
// `hscc cluster monitor` (host table + serving table) and
// `hscc cluster status` (workload count / total hosts). This file projects
// exactly those fields; the views render the strings this file returns and
// compose no numbers of their own.
//
// Reads (the §2 budget — two per refresh for every family):
//   GET /v1/cluster/monitor — hosts[].error / sample (node state, GPU,
//     CPU/GPU temps) and serving[] (per-node model, run/wait, Gen t/s).
//   GET /v1/cluster/status — the fleet power state. It is the one word the
//     remaining surfaces use to say "Serving"/"Waking"/"Down"; the node and
//     model rows above are the CLI truth either way.
//   ...and for `systemSmall` ONLY (t_2681d3de) the status read is swapped for
//     GET /v1/cluster/history, the recorder's stored series behind the tile's
//     GPU% sparklines. The provider picks one or the other per family, so no
//     family ever sends more than the two reads §2 allows.
//
// Removed from the old surface: the hard-coded four-node topology glyph (four
// invented node identities), the load average (the CLI's host
// table does not print it), the board/kanban row (that is not what
// `hscc cluster monitor` shows), and the per-metric dashes that made a
// degraded read look like a row of zeroes.
// ===========================================================================

/// What the monitor payload says about the fleet, reduced to the CLI's rows.
/// `FleetProjection` (Shared) owns the projection; this alias is the widget's
/// stable name for it.
typealias ClusterFleet = FleetProjection

/// How hot a percentage or temperature reads, in the app's OWN display bands.
/// Not a hardware spec: the number is always printed beside the colour.
enum ClusterMetricHeat: Equatable {
    case calm
    case warm
    case hot
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

    /// A serving node's GPU is the number an operator watches; a node that did
    /// not answer is not "calm".
    static func node(_ node: FleetNodeLine) -> ClusterMetricHeat {
        guard node.isUp else { return .hot }
        return percent(node.gpuPct)
    }
}

// ---------------------------------------------------------------------------
// The entry. Gate states are first class: unconfigured and paused each say so,
/// and a failed read carries its reason plus the age of the last good read.
// ---------------------------------------------------------------------------

/// The one stale/offline rule every widget runs.
///
/// Stale means the DATA is older than twice the surface's refresh interval; it
/// dims to 50% and says "stale · HH:mm". A failed fetch says "offline · HH:mm"
/// and dims whatever cached content the caller still has. Neither state ever
/// renders cached data as fresh.
// MARK: - C1 Health line (2026-11-20 redesign)

/// One node row in the new Cluster widget: identity, state, and the four
/// numbers C1 prints beside it. Every field is a route field (or the serving
/// row that names the node's backend); no view derives a number of its own.
struct ClusterC1Node: Equatable, Identifiable {
    let label: String
    let model: String?
    let isUp: Bool
    let isOrchestrator: Bool
    let gpuPct: Double?
    let tempC: Double?
    let powerW: Double?
    let genTps: Double?

    var id: String { label }

    init(_ node: FleetNodeLine) {
        self.label = node.label
        self.model = node.model
        self.isUp = node.isUp
        self.isOrchestrator = node.isOrchestrator
        self.gpuPct = node.gpuPct
        self.tempC = node.gpuTempC ?? node.cpuTempC
        self.powerW = node.gpuPowerW
        self.genTps = node.genTps
    }
}

/// One point of the large tile's 2 h total-throughput chart: the sum of every
/// engine's `gen_tps` at that recorder timestamp.
struct ClusterThroughputPoint: Equatable {
    let t: TimeInterval
    let value: Double
}

enum ClusterThroughput {
    /// Sum the engine series by timestamp. A timestamp with no readable engine
    /// value is a gap, not a zero. Nil/empty response means no chart.
    static func series(_ response: ClusterHistoryResponse?) -> [ClusterThroughputPoint] {
        guard let response else { return [] }
        let set = ClusterHistorySeriesSet.make(response, for: .genTps)
        var sums: [TimeInterval: Double] = [:]
        for line in set.series {
            for sample in line.samples {
                guard let value = sample.value else { continue }
                sums[sample.t, default: 0] += value
            }
        }
        let ordered = sums.keys.sorted().map { ClusterThroughputPoint(t: $0, value: sums[$0] ?? 0) }
        guard let last = ordered.last?.t else { return [] }
        return ordered.filter { $0.t >= last - 2 * 60 * 60 }
    }
}

/// Everything C1 draws, projected once from the entry's one monitor read.
struct ClusterC1: Equatable {
    let up: Int?
    let total: Int?
    let tokPerSec: Double?
    let hottestC: Double?
    let nodes: [ClusterC1Node]
    let modelNames: [String]
    let daemonRunning: Bool?
    let dataAt: Date?
    let fetchedAt: Date
    let refreshMinutes: Int
    let failed: Bool

    var headline: String {
        guard let up, let total else { return failed ? "Offline" : "No data" }
        if total > 0 && up == 0 { return "Down" }
        if up == total { return "All up" }
        return "\(up)/\(total) up"
    }

    var tally: String {
        guard let up, let total else { return "—" }
        return "\(up)/\(total)"
    }

    var tokText: String {
        guard let tokPerSec else { return "— tok/s" }
        return "\(Int(tokPerSec.rounded())) tok/s"
    }

    var hottestText: String {
        guard let hottestC else { return "Hottest —" }
        return "Hottest \(Int(hottestC.rounded()))°"
    }

    // MARK: L2 Lock Screen copy
    //
    // The three accessory families read these values. They are computed here
    // rather than in a body so the executed gate can pin the exact strings and
    // numbers a Lock Screen draws without a simulator.

    /// The hottest node and its reading, or nil when no node reported one.
    /// A missing temperature keeps the ring empty instead of claiming 0 °C.
    var hottestNode: ClusterC1Node? {
        nodes.filter { $0.tempC != nil }
            .max { ($0.tempC ?? 0) < ($1.tempC ?? 0) }
    }

    /// The gauge fill, clamped to the L2 contract's 0...90 °C range.
    var hottestGaugeValue: Double? {
        hottestNode?.tempC.map { min(max($0, 0), 90) }
    }

    /// `accessoryInline`: "4/4 up · N tok/s".
    var inlineL2: String {
        guard up != nil, total != nil else { return failed ? "Offline" : "No data" }
        return "\(tally) up · \(tokText)"
    }

    /// `accessoryRectangular`: "CLUSTER · N TOK/S".
    var rectangularHeadline: String {
        "CLUSTER · " + (tokPerSec.map { "\(Int($0.rounded())) TOK/S" } ?? "— TOK/S")
    }

    /// The four node temperatures the rectangular row draws, in route order.
    var rectangularTemps: [String] {
        nodes.prefix(4).map { node in
            node.tempC.map { "\(Int($0.rounded()))°" } ?? "—"
        }
    }

    /// The footer's model line: `name ×N` per model, in route order.
    var modelLine: String {
        var order: [String] = []
        var counts: [String: Int] = [:]
        for node in nodes {
            guard let model = node.model else { continue }
            if counts[model] == nil { order.append(model) }
            counts[model, default: 0] += 1
        }
        if order.isEmpty { return "no models" }
        return order.map { "\($0) ×\(counts[$0] ?? 0)" }.joined(separator: " · ")
    }

    var daemonText: String {
        switch daemonRunning {
        case .some(true): return "daemon OK"
        case .some(false): return "daemon down"
        case .none: return "daemon —"
        }
    }

    var stale: StaleState {
        StaleState(dataAt: dataAt, fetchedAt: fetchedAt,
                   refreshMinutes: refreshMinutes, failed: failed)
    }

    init(_ entry: ClusterEntry) {
        self.up = entry.fleet?.tally.nodesUp
        self.total = entry.fleet?.tally.nodesTotal
        self.tokPerSec = entry.fleet?.serving.genTps
        self.nodes = entry.fleet?.nodes.map(ClusterC1Node.init) ?? []
        self.hottestC = nodes.compactMap(\.tempC).max()
        self.modelNames = entry.fleet?.serving.modelNames ?? []
        self.daemonRunning = entry.daemonHost?.daemon_running
        self.dataAt = entry.dataAt
        self.fetchedAt = entry.date
        self.refreshMinutes = entry.refreshMinutes
        self.failed = entry.failure != nil
    }
}

/// `hscc cluster status`'s counts, reduced to the two numbers the widget draws.
struct ClusterStatusSummary: Equatable {
    let jobs: Int
    let idleHosts: Int
    let totalHosts: Int

    init?(_ response: ClusterStatusResponse?) {
        guard let response, response.error == nil else { return nil }
        self.jobs = response.workloads.count
        self.idleHosts = response.idle_hosts.count
        self.totalHosts = response.total_hosts
    }
}

struct ClusterEntry: TimelineEntry {
    let date: Date
    /// The CLI rows, or nil when the monitor route sent no host list.
    let fleet: ClusterFleet?
    /// `hscc cluster monitor`'s host table for this refresh — the nine
    /// already-formatted cells per host (`CLIMonitorHostRow`), or nil when the
    /// monitor read did not answer. `fleet` above is what the glance surfaces
    /// project and this is what the CLI-shaped medium tile draws, and both come
    /// out of the SAME decoded response, so the headline tally and the table
    /// under it cannot disagree about how many hosts there are.
    let monitor: ClusterMonitorViewModel?
    /// `hscc cluster status`'s counts, or nil when that read did not answer.
    let status: ClusterStatusSummary?
    /// The two tables the large/extra-large surfaces draw (t_9e64d9d9),
    /// projected from the SAME monitor read as `fleet` — never a second request.
    /// Nil when that read sent no host list, which is the same condition `fleet`
    /// is nil under. Named apart from `monitor` because the two are different
    /// projections of that one read: `monitor` is the CLI view-model the medium
    /// table and the small grid draw, this is the large/extra-large LAYOUT.
    let tableData: ClusterMonitorTables?
    /// What stopped this refresh being live. Nil while live.
    let failure: WidgetDataFailure?
    let configured: Bool
    let paused: Bool
    /// Age of the last successful read, shown as "updated Nm ago". Set only
    /// when this entry is showing last-known/offline content.
    let lastKnownAgeMinutes: Int?
    /// The recorder's series behind each host's GPU% sparkline (t_2681d3de):
    /// the `GET /v1/cluster/history` answer for this refresh. Nil whenever the
    /// read was not made (every family except `systemSmall`), failed, or came
    /// back unusable — and nil is honestly "no trend": the GPU cells draw the
    /// single-sample bars they drew before, never a flat line. A failed
    /// history read deliberately does NOT set `failure` either; the trend is
    /// an extra the tile can live without, and the rows above it are live.
    let history: ClusterHistoryResponse?
    /// Operator content toggles. Only "nodes" is offered/drawn; the old
    /// "state"/"topology"/"models"/"board" values were dead controls (audit
    /// W11) and no longer exist in `availableSections`.
    let showSections: Set<String>
    /// The four Lock Screen metrics, projected from the CLI view-model over the
    /// SAME monitor read as `fleet`. Nil when that read never answered — the
    /// accessory families then fall back to the failure/last-known copy below
    /// rather than drawing a row of dashes.
    let lockScreen: CLILockScreenMetrics?
    /// When the DATA was sampled, for AsOfStamp / the stale rule. Falls back to
    /// the fetch time when the route sends no usable timestamp.
    let dataAt: Date?
    /// The surface's configured cadence, for the 2× stale rule.
    let refreshMinutes: Int
    /// The daemon-host sidecar, read only by sizes that draw its state.
    let daemonHost: MonitorDaemonHost?
    /// The large tile's 2 h total-throughput series, from the same history read
    /// the old small tile used. Empty means "no chart", never a flat zero line.
    let throughput: [ClusterThroughputPoint]

    init(date: Date,
         fleet: ClusterFleet?,
         monitor: ClusterMonitorViewModel? = nil,
         status: ClusterStatusSummary? = nil,
         tableData: ClusterMonitorTables? = nil,
         failure: WidgetDataFailure? = nil,
         configured: Bool,
         paused: Bool,
         lastKnownAgeMinutes: Int? = nil,
         showSections: Set<String> = ClusterEntry.availableSections,
         lockScreen: CLILockScreenMetrics? = nil,
         history: ClusterHistoryResponse? = nil,
         dataAt: Date? = nil,
         refreshMinutes: Int = 30,
         daemonHost: MonitorDaemonHost? = nil,
         throughput: [ClusterThroughputPoint] = []) {
        self.date = date
        self.fleet = fleet
        self.monitor = monitor
        self.status = status
        self.tableData = tableData
        self.failure = failure
        self.configured = configured
        self.paused = paused
        self.lastKnownAgeMinutes = lastKnownAgeMinutes
        self.history = history
        self.showSections = showSections
        self.lockScreen = lockScreen
        self.dataAt = dataAt
        self.refreshMinutes = refreshMinutes
        self.daemonHost = daemonHost
        self.throughput = throughput
    }

    static let availableSections = WidgetSurfaceKind.widgetCluster.availableSections

    /// What the small tile draws (t_9e110d88): four of the CLI's host columns
    /// across up to four hosts, plus the serving totals — and since t_2681d3de,
    /// each host's GPU% cell carries the recorder-backed trend when this
    /// entry's `history` answer has one for it. Nil means there is nothing to
    /// grid — the family then shows its gate pane (setup invite, "Paused", or
    /// why the read failed), never a table of dashes.
    var smallTile: ClusterSmallGrid.Tile? {
        ClusterSmallGrid.tile(from: monitor, history: history)
    }

    /// The tables a large/extra-large body may draw, or nil. This is the SAME
    /// gate `text(for:)` runs first: an unconfigured or paused surface has no
    /// table, so no family can paint a live-looking table where the text model
    /// would have printed the invite. Checking it here rather than in each body
    /// keeps one owner for the rule.
    var tables: ClusterMonitorTables? {
        (!configured || paused) ? nil : tableData
    }

    /// Everything the 2026-11-20 C1 layout draws, from this entry's one read.
    var c1: ClusterC1 { ClusterC1(self) }

    /// The headline colour: green only when every listed node answered, amber
    /// when some are down, red when none answered. The label beside it is the
    /// tally, not a second state word.
    var heat: ClusterMetricHeat {
        guard let fleet, let up = fleet.tally.nodesUp, let total = fleet.tally.nodesTotal else {
            return .unknown
        }
        if up == 0 && total > 0 { return .hot }
        return up == total ? .calm : .warm
    }

    /// The host rows a CLI-shaped surface (the medium tile) may draw, or nil
    /// when something else owns the surface: the device is unconfigured, the
    /// surface is paused, the operator switched the "nodes" section off, or the
    /// read carried no host list.
    ///
    /// Nil and an empty array are deliberately different (§6 rule 1): nil means
    /// "there is no table here, draw the entry's own gate or failure copy",
    /// while a table with zero rows would read as "the route says no hosts".
    /// The gate states are re-checked here rather than trusting the caller,
    /// because a family that reached `monitor` directly would be a family that
    /// draws numbers on a paused surface.
    var cliHostRows: [CLIMonitorHostRow]? {
        guard configured, !paused, showSections.contains("nodes") else { return nil }
        guard let monitor, !monitor.hostRows.isEmpty else { return nil }
        return monitor.hostRows
    }

    /// The one short string an accessory renders when it has nothing live.
    var shortGateLabel: String {
        if !configured { return "Set up in HSCC" }
        if paused { return "Paused" }
        return failure?.shortLabel ?? "No data"
    }

    /// The text every family draws. System families get the operator sentence;
    /// Lock Screen accessories get the short label, because one line is all
    /// they have.
    func text(for surface: FleetSurface) -> FleetText {
        if !configured || paused {
            return FleetText(headline: shortGateLabel, detail: nil, rows: [], footer: nil)
        }
        let shortForm = surface == .small || surface == .inline
            || surface == .circular || surface == .rectangular
        let reason = shortForm ? failure?.shortLabel : failure?.operatorLine
        // The three Lock Screen families draw the CLI's own four numbers —
        // hottest temperature, GPU load, fleet watts, running/waiting — when
        // this refresh has them. `CLILockScreenCopy.text` returns nil for a
        // system family, so this one branch cannot reach small/medium/large,
        // and a nil `lockScreen` (a monitor read that never answered) falls
        // through to the tally / failure copy below instead of drawing dashes.
        if let lockScreen, !lockScreen.hasNothing,
           let cli = CLILockScreenCopy.text(for: surface, lockScreen,
                                            failure: reason,
                                            ageMinutes: lastKnownAgeMinutes) {
            return cli
        }
        guard let fleet else {
            if let status {
                let detail = FleetFormat.count(status.jobs, singular: "job", plural: "jobs")
                    + " · " + FleetFormat.count(status.totalHosts, singular: "host", plural: "hosts")
                return FleetText(headline: reason ?? "No node sample",
                                 detail: detail,
                                 rows: [],
                                 footer: FleetFormat.age(lastKnownAgeMinutes))
            }
            return FleetText(headline: reason ?? "No data", detail: nil, rows: [],
                             footer: FleetFormat.age(lastKnownAgeMinutes))
        }
        return fleet.text(for: surface, failure: reason, ageMinutes: lastKnownAgeMinutes)
    }

    var inlineLine: String {
        text(for: .inline).headline + (text(for: .inline).detail.map { " · \($0)" } ?? "")
    }

    /// The value the Lock Screen ring fills to: the fleet's GPU load, 0…100.
    /// Nil means the ring has nothing to measure and draws the gated line
    /// instead — a ring at zero would claim an idle fleet (§6 rule 1).
    var lockScreenGaugeValue: Double? {
        lockScreen.flatMap { $0.gpuUtil?.value }
    }

    /// The VoiceOver sentence, and only when the Lock Screen copy is what is on
    /// screen: a fallback line ("Timed out") must not be read out as a list of
    /// things nobody measured.
    var lockScreenVoice: String? {
        guard let lockScreen, !lockScreen.hasNothing else { return nil }
        return CLILockScreenCopy.voice(lockScreen)
    }

    static let unconfigured = ClusterEntry(date: .now,
                                           fleet: nil,
                                           configured: false,
                                           paused: false)

    static func paused(_ date: Date = .now) -> ClusterEntry {
        ClusterEntry(date: date, fleet: nil, configured: true, paused: true)
    }

    /// First-render / gallery data. Sample only: `placeholder(in:)` never runs
    /// through the provider's fetch path and can never replace a real entry.
    /// It carries the CLI table AND the large/XL tables, so the gallery shows
    /// the medium tile as the operator will meet it rather than as an empty
    /// frame, and a preview of a size that draws tables shows tables — the same
    /// projections the live path uses, over sample data, so the preview cannot
    /// drift from what ships.
    static let placeholder = ClusterEntry(date: .now,
                                          fleet: sampleFleet,
                                          monitor: sampleMonitor,
                                          tableData: ClusterMonitorTables.project(sampleResponse),
                                          configured: true,
                                          paused: false,
                                          lockScreen: sampleLockScreen)

    /// One sample payload, projected FOUR ways from one `MonitorResponse`: the
    /// glance projection the small/accessory surfaces draw, the CLI cell table
    /// the medium tile draws, the large/extra-large table layout, and the Lock
    /// Screen's four numbers. A second, hand-typed sample would be a second truth
    /// about what the fleet looks like, and the gallery would then show a
    /// headline tally, a table and a Lock Screen line that do not describe the
    /// same cluster. Addresses are TEST-NET only (§7).
    private static let sampleResponse = MonitorResponse(
        json: MonitorPayload(timestamp: nil, hosts: sampleHosts),
        serving: sampleServing,
        serving_error: nil,
        serving_proxy_error: nil,
        speak: "sample")

    private static let sampleFleet = FleetProjection(projecting: sampleResponse)

    private static let sampleMonitor = ClusterMonitorViewModel.project(sampleResponse)

    /// The Lock Screen sample comes from the same bytes, through the same
    /// projection the shipped widgets run — a hand-written sample line here
    /// would be a second copy rule nobody executes.
    private static let sampleLockScreen = CLILockScreenMetrics(sampleResponse)

    /// The sample hosts, in route order. The values cover the threshold bands
    /// the medium tile paints — a calm pair, a warm one (CPU 87.3 %, RAM 91.2 %,
    /// 84.9 °C, 93.7 % of the power limit) and a critical one (91.2 °C, 88 °C,
    /// 98.3 % of the limit) — plus the two ways Swap goes to `-`: no swap
    /// configured (`swap_total_mb` "0") and the metric never read (absent).
    private static let sampleHosts: [MonitorNode] = {
        func sample(_ hostname: String, jobs: String, cpu: String, ram: String,
                    swapUsed: String?, swapTotal: String?, gpu: String,
                    gpuTemp: String, cpuTemp: String,
                    power: String, powerLimit: String) -> MonitorSample {
            MonitorSample(hostname: hostname,
                          cpu_usage_pct: cpu,
                          mem_used_pct: ram,
                          gpu_util_pct: gpu,
                          cpu_temp_c: cpuTemp,
                          gpu_temp_c: gpuTemp,
                          swap_total_mb: swapTotal,
                          swap_used_mb: swapUsed,
                          gpu_power_w: power,
                          gpu_power_limit_w: powerLimit,
                          sparkrun_jobs: jobs)
        }
        func host(_ address: String, _ label: String, jobs: String, cpu: String,
                  ram: String, swapUsed: String?, swapTotal: String?, gpu: String,
                  gpuTemp: String, cpuTemp: String, power: String,
                  workload: String) -> MonitorNode {
            MonitorNode(host: address, error: nil,
                        sample: sample(label, jobs: jobs, cpu: cpu, ram: ram,
                                       swapUsed: swapUsed, swapTotal: swapTotal,
                                       gpu: gpu, gpuTemp: gpuTemp, cpuTemp: cpuTemp,
                                       power: power, powerLimit: "200"),
                        used_slots: nil, free_slots: nil,
                        workloads: [MonitorWorkload(workload_id: workload, cluster_id: nil)])
        }
        return [
            host("192.0.2.11", "node-1", jobs: "1", cpu: "12.4", ram: "45.6",
                 swapUsed: "0", swapTotal: "2048", gpu: "0",
                 gpuTemp: "63", cpuTemp: "70", power: "12.50", workload: "a"),
            host("192.0.2.12", "node-2", jobs: "0", cpu: "8.1", ram: "91.2",
                 swapUsed: "128", swapTotal: "2048", gpu: "0",
                 gpuTemp: "63", cpuTemp: "68", power: "9.75", workload: "a"),
            host("192.0.2.13", "node-3", jobs: "1", cpu: "87.3", ram: "62.0",
                 swapUsed: nil, swapTotal: "0", gpu: "96",
                 gpuTemp: "78", cpuTemp: "84.9", power: "187.4", workload: "b"),
            host("192.0.2.14", "node-4", jobs: "0", cpu: "5.0", ram: "10.0",
                 swapUsed: "0", swapTotal: nil, gpu: "0",
                 gpuTemp: "88", cpuTemp: "91.2", power: "196.5", workload: "b"),
        ]

    }()

    private static let sampleServing: [ServingRow] = [
        ServingRow(node: "192.0.2.11", port: "8001", model: "model-a", routes: [],
                   running: 0, waiting: 0, kv: 0.2, gen_tps: 6.1, prompt_tps: 10,
                   prompt_total: nil, gen_total: nil, error: nil),
        ServingRow(node: "192.0.2.13", port: "8002", model: "model-b", routes: [],
                   running: 3, waiting: 0, kv: 0.4, gen_tps: 6.2, prompt_tps: 12,
                   prompt_total: nil, gen_total: nil, error: nil),
    ]
}
