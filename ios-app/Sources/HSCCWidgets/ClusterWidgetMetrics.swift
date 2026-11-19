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
// Reads (two, the §2 budget):
//   * GET /v1/cluster/monitor — hosts[].error / sample (node state, GPU,
//     CPU/GPU temps) and serving[] (per-node model, run/wait, Gen t/s).
//   * GET /v1/autodown/status — the fleet power state. It is the one word the
//     remaining surfaces use to say "Serving"/"Waking"/"Down"; the node and
//     model rows above are the CLI truth either way.
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
    /// `hscc cluster status`'s counts, or nil when that read did not answer.
    let status: ClusterStatusSummary?
    /// The two tables the large/extra-large surfaces draw, projected from the
    /// SAME monitor read as `fleet` — never a second request. Nil when that read
    /// sent no host list, which is the same condition `fleet` is nil under.
    let monitor: ClusterMonitorTables?
    /// What stopped this refresh being live. Nil while live.
    let failure: WidgetDataFailure?
    let configured: Bool
    let paused: Bool
    /// Age of the last successful read, shown as "updated Nm ago". Set only
    /// when this entry is showing last-known/offline content.
    let lastKnownAgeMinutes: Int?
    /// Operator content toggles. Only "nodes" and "models" are drawn; the old
    /// "topology"/"board" values are kept in the shared model for the settings
    /// editor but no longer have pixels here.
    let showSections: Set<String>

    init(date: Date,
         fleet: ClusterFleet?,
         status: ClusterStatusSummary? = nil,
         monitor: ClusterMonitorTables? = nil,
         failure: WidgetDataFailure? = nil,
         configured: Bool,
         paused: Bool,
         lastKnownAgeMinutes: Int? = nil,
         showSections: Set<String> = ClusterEntry.availableSections) {
        self.date = date
        self.fleet = fleet
        self.status = status
        self.monitor = monitor
        self.failure = failure
        self.configured = configured
        self.paused = paused
        self.lastKnownAgeMinutes = lastKnownAgeMinutes
        self.showSections = showSections
    }

    static let availableSections = WidgetSurfaceKind.widgetCluster.availableSections

    /// The tables a large/extra-large body may draw, or nil. This is the SAME
    /// gate `text(for:)` runs first: an unconfigured or paused surface has no
    /// table, so no family can paint a live-looking table where the text model
    /// would have printed the invite. Checking it here rather than in each body
    /// keeps one owner for the rule.
    var tables: ClusterMonitorTables? {
        (!configured || paused) ? nil : monitor
    }

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

    static let unconfigured = ClusterEntry(date: .now,
                                           fleet: nil,
                                           configured: false,
                                           paused: false)

    static func paused(_ date: Date = .now) -> ClusterEntry {
        ClusterEntry(date: date, fleet: nil, configured: true, paused: true)
    }

    /// Placeholder/first-render data. Sample only: `placeholder(in:)` never runs
    /// through the provider's fetch path and can never replace a real entry.
    /// It carries the tables too, because a gallery preview of a size that draws
    /// tables must show tables — the same projection the live path uses, over
    /// sample data, so the preview cannot drift from what ships.
    static let placeholder = ClusterEntry(date: .now,
                                          fleet: sampleFleet,
                                          monitor: ClusterMonitorTables.project(sampleResponse),
                                          configured: true,
                                          paused: false)

    /// TEST-NET addresses only (§7): a sample must never look like a real node.
    private static let sampleResponse = MonitorResponse(
        json: MonitorPayload(timestamp: nil, hosts: sampleHosts),
        serving: sampleServing,
        serving_error: nil,
        serving_proxy_error: nil,
        speak: "sample")

    private static let sampleFleet: ClusterFleet? = FleetProjection(projecting: sampleResponse)

    private static let sampleHosts: [MonitorNode] = {
        func sample(_ hostname: String, gpu: String, gpuTemp: String, cpuTemp: String) -> MonitorSample {
            MonitorSample(hostname: hostname, uptime_sec: nil, cpu_usage_pct: nil,
                          mem_used_pct: nil, mem_total_mb: nil, mem_used_mb: nil,
                          gpu_name: nil, gpu_util_pct: gpu, gpu_mem_used_pct: nil,
                          cpu_temp_c: cpuTemp, gpu_temp_c: gpuTemp, cpu_load_1m: nil)
        }
        let hosts = [
            MonitorNode(host: "192.0.2.11", error: nil,
                        sample: sample("node-1", gpu: "0", gpuTemp: "63", cpuTemp: "70"),
                        used_slots: nil, free_slots: nil,
                        workloads: [MonitorWorkload(workload_id: "a", cluster_id: nil)]),
            MonitorNode(host: "192.0.2.12", error: nil,
                        sample: sample("node-2", gpu: "0", gpuTemp: "63", cpuTemp: "68"),
                        used_slots: nil, free_slots: nil,
                        workloads: [MonitorWorkload(workload_id: "a", cluster_id: nil)]),
            MonitorNode(host: "192.0.2.13", error: nil,
                        sample: sample("node-3", gpu: "96", gpuTemp: "78", cpuTemp: "87"),
                        used_slots: nil, free_slots: nil,
                        workloads: [MonitorWorkload(workload_id: "b", cluster_id: nil)]),
            MonitorNode(host: "192.0.2.14", error: nil,
                        sample: sample("node-4", gpu: "96", gpuTemp: "74", cpuTemp: "82"),
                        used_slots: nil, free_slots: nil,
                        workloads: [MonitorWorkload(workload_id: "b", cluster_id: nil)]),
        ]
        return hosts
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
