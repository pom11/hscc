import SwiftUI
import WidgetKit

// ---------------------------------------------------------------------------
// HSCC Monitor widget — the same two routes the Live Activity reads:
//
//   GET /v1/cluster/monitor — hosts[] (CPU/RAM/GPU + CPU/GPU temps) and
//                             serving[] (model per node, run/wait, Gen t/s)
//   GET /v1/daemon/host    — the machine HSCC runs on, including the daemon
//                             health block `hscc status` prints
//
// The old surface drew a 2×2 grid of three bars per node and an averaged CPU
// gauge in the circular family. Neither is a CLI row: the average is a number
// the CLI never prints, and the grid left no room for the model the node is
// actually serving. This version renders the CLI rows through `FleetText` and
// spends the saved space on the model, the temperature and the daemon state.
// ---------------------------------------------------------------------------

enum MonitorState {
    case monitoring
    case degraded
    case unreachable
    case unconfigured
    case paused

    func color(_ style: WidgetStyle) -> Color {
        switch self {
        case .monitoring: return style.okColor
        case .degraded: return style.warnColor
        case .unreachable, .unconfigured, .paused: return style.secondaryTextColor
        }
    }

    var label: String {
        switch self {
        case .monitoring: return "Monitoring"
        case .degraded: return "Degraded"
        case .unreachable: return "Can't reach the cluster"
        case .unconfigured: return "Set up the app to see the cluster"
        case .paused: return "Paused in Settings"
        }
    }
}

struct MonitorEntry: TimelineEntry {
    let date: Date
    let state: MonitorState
    let configured: Bool
    let paused: Bool
    /// The CLI rows (nodes + serving table), or nil when the monitor route
    /// sent no host list.
    let fleet: ClusterFleet?
    /// The two tables the large/extra-large surfaces draw — the same
    /// `GET /v1/cluster/monitor` response as `fleet`, never a third read.
    let monitor: ClusterMonitorTables?
    /// The daemon-host machine data, or nil when that read did not answer.
    let daemonHost: MonitorDaemonHost?
    let failure: WidgetDataFailure?
    let refreshMinutes: Int
    let showSections: Set<String>
    let lastKnownAgeMinutes: Int?
    let lastKnownNodeCount: Int

    static let availableSections = WidgetSurfaceKind.widgetMonitor.availableSections

    /// The tables a large/extra-large body may draw, or nil — the same gate
    /// `text(for:)` runs first, so a paused or unconfigured tile cannot paint a
    /// live-looking table. One owner for the rule, not one per body.
    var tables: ClusterMonitorTables? {
        (!configured || paused) ? nil : monitor
    }

    var shortGateLabel: String {
        if !configured { return "Set up in HSCC" }
        if paused { return "Paused" }
        return failure?.shortLabel ?? "No data"
    }

    /// The daemon's health sentence: `hscc status`'s daemon block, with the
    /// host metrics the route carries beside it.
    func daemonLine(compact: Bool = false) -> String? {
        guard let host = daemonHost else { return nil }
        let name = host.hostname?.isEmpty == false ? host.hostname! : "Daemon host"
        let health = host.daemon_running == true ? "daemon up" : "daemon down"
        if compact {
            var parts = [health]
            if let cpu = host.cpuPercent { parts.append("CPU \(Int(cpu.rounded()))%") }
            if let mem = host.memPercent { parts.append("RAM \(Int(mem.rounded()))%") }
            return "\(name) · " + parts.joined(separator: " · ")
        }
        var parts: [String] = []
        if let platform = host.platform, let arch = host.arch {
            parts.append("\(platform) \(arch)")
        }
        if let cpu = host.cpuPercent { parts.append("CPU \(Int(cpu.rounded()))%") }
        if let mem = host.memPercent { parts.append("RAM \(Int(mem.rounded()))%") }
        if let disk = host.diskPercent { parts.append("Disk \(Int(disk.rounded()))%") }
        if let up = host.daemonUptimeText { parts.append("daemon up \(up)") }
        return "\(name) · " + (parts.isEmpty ? health : parts.joined(separator: " · "))
    }

    /// The text every family draws. Node rows come from `FleetText`; the
    /// daemon line is appended as the footer (system) or the second line
    /// (Lock Screen).
    func text(for surface: FleetSurface) -> FleetText {
        if !configured || paused {
            return FleetText(headline: shortGateLabel, detail: nil, rows: [], footer: nil)
        }
        let isAccessory = surface == .inline || surface == .circular || surface == .rectangular
        let shortForm = surface == .small || isAccessory
        let reason = shortForm ? failure?.shortLabel : failure?.operatorLine
        let base: FleetText
        if let fleet {
            base = fleet.text(for: surface, failure: reason, ageMinutes: lastKnownAgeMinutes)
        } else if let host = daemonHost {
            let health = host.daemon_running == true ? "Daemon up" : "Daemon down"
            base = FleetText(headline: reason ?? health,
                             detail: host.hostname,
                             rows: [],
                             footer: FleetFormat.age(lastKnownAgeMinutes))
        } else {
            base = FleetText(headline: reason ?? "No data", detail: nil, rows: [],
                             footer: FleetFormat.age(lastKnownAgeMinutes))
        }
        guard showSections.contains("host"), let daemon = daemonLine(compact: isAccessory) else {
            return base
        }
        if isAccessory {
            let detail = [base.detail, daemon].compactMap { $0 }.joined(separator: " · ")
            return FleetText(headline: base.headline, detail: detail, rows: base.rows,
                             footer: base.footer)
        }
        let footer = [base.footer, daemon].compactMap { $0 }.joined(separator: " · ")
        return FleetText(headline: base.headline, detail: base.detail, rows: base.rows,
                         footer: footer.isEmpty ? nil : footer)
    }

    var inlineLine: String {
        let text = text(for: .inline)
        return [text.headline, text.detail].compactMap { $0 }.joined(separator: " · ")
    }

    static let unconfigured = MonitorEntry(date: .now, state: .unconfigured,
                                           configured: false, paused: false,
                                           fleet: nil, monitor: nil, daemonHost: nil,
                                           failure: nil, refreshMinutes: 5,
                                           showSections: availableSections,
                                           lastKnownAgeMinutes: nil,
                                           lastKnownNodeCount: 0)

    static func paused(_ date: Date = .now) -> MonitorEntry {
        MonitorEntry(date: date, state: .paused, configured: true, paused: true,
                     fleet: nil, monitor: nil, daemonHost: nil, failure: nil, refreshMinutes: 0,
                     showSections: availableSections,
                     lastKnownAgeMinutes: nil, lastKnownNodeCount: 0)
    }

    static let placeholder = MonitorEntry(date: .now, state: .monitoring,
                                          configured: true, paused: false,
                                          fleet: ClusterEntry.placeholder.fleet,
                                          monitor: ClusterEntry.placeholder.monitor,
                                          daemonHost: sampleDaemonHost,
                                          failure: nil, refreshMinutes: 5,
                                          showSections: availableSections,
                                          lastKnownAgeMinutes: nil,
                                          lastKnownNodeCount: 0)

    private static let sampleDaemonHost = MonitorDaemonHost(
        hostname: "hscc-host", platform: "macOS", arch: "arm64",
        uptime_seconds: 86400,
        cpu: MonitorDaemonCPU(count: 14, percent: 11, load_avg: []),
        memory: MonitorDaemonMemory(total_gb: 36, used_gb: 18, percent: 50),
        disk: MonitorDaemonDisk(total_gb: 460, used_gb: 13, percent: 7),
        processes: 800, daemon_running: true, daemon_uptime_seconds: 3600,
        speak: "sample")
}

// ---------------------------------------------------------------------------
// Timeline provider
// ---------------------------------------------------------------------------

struct MonitorTimelineProvider: TimelineProvider {
    func placeholder(in context: Context) -> MonitorEntry { .placeholder }

    func getSnapshot(in context: Context, completion: @escaping (MonitorEntry) -> Void) {
        Task { completion(await fetchEntry(family: context.family)) }
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<MonitorEntry>) -> Void) {
        Task {
            let entry = await fetchEntry(family: context.family)
            guard entry.refreshMinutes > 0 else {
                completion(Timeline(entries: [entry], policy: .atEnd))
                return
            }
            let next = Calendar.current.date(byAdding: .minute, value: entry.refreshMinutes, to: .now)
                ?? .now.addingTimeInterval(300)
            completion(Timeline(entries: [entry], policy: .after(next)))
        }
    }

    private func fetchEntry(family: WidgetFamily) async -> MonitorEntry {
        let surface = AppGroupWidgetSettings.load()?.setting(for: .widgetMonitor)
        let refreshMinutes = surface?.refreshMinutes ?? 5
        let showSections = surface?.showSections ?? MonitorEntry.availableSections
        if let surface, !surface.isActive { return .paused() }

        guard let client = WidgetDataClient.makeIfConfigured() else {
            return .unconfigured
        }

        // Accessories draw nodes + serving only; the daemon-host read is
        // skipped entirely for them (§2's budget, same rule as before).
        let wantsHost = family.showsDaemonHost
        async let monitorTask = WidgetData.read(using: client) { await $0.clusterMonitor() }
        async let hostTask = Self.daemonHost(client: client, enabled: wantsHost)
        let (monitorOutcome, hostOutcome) = await (monitorTask, hostTask)

        let fleet = monitorOutcome.value.flatMap(FleetProjection.init(projecting:))
        let daemonHost = hostOutcome?.value
        let failure = monitorOutcome.failure ?? hostOutcome?.failure
        let anyData = fleet != nil || daemonHost != nil

        guard anyData else {
            let last = MonitorSnapshotStore.load()
            let age = last.map { WidgetData.ageMinutes(from: Date(timeIntervalSince1970: $0.date)) }
            return MonitorEntry(date: .now, state: .unreachable, configured: true,
                                paused: false, fleet: nil, monitor: nil,
                                daemonHost: last?.daemonHost,
                                failure: failure, refreshMinutes: refreshMinutes,
                                showSections: showSections,
                                lastKnownAgeMinutes: failure?.showsStaleData == true ? age : nil,
                                lastKnownNodeCount: last?.nodes.count ?? 0)
        }

        let down = fleet?.tally.nodesDown ?? 0
        let state: MonitorState = down > 0 ? .degraded : .monitoring

        // Persist the projection only (never the wire node with its address).
        if case .live = monitorOutcome, let nodes = monitorOutcome.value?.nodes {
            let snapshotHost = wantsHost ? daemonHost : MonitorSnapshotStore.load()?.daemonHost
            MonitorSnapshotStore.save(nodes: MonitorSnapshotStore.project(nodes),
                                      daemonHost: snapshotHost)
        }

        return MonitorEntry(date: .now, state: state, configured: true, paused: false,
                            fleet: fleet,
                            monitor: ClusterMonitorTables.project(monitorOutcome.value),
                            daemonHost: daemonHost, failure: failure,
                            refreshMinutes: refreshMinutes, showSections: showSections,
                            lastKnownAgeMinutes: nil, lastKnownNodeCount: 0)
    }

    private static func daemonHost(client: WidgetDataClient,
                                   enabled: Bool) async -> WidgetDataOutcome<MonitorDaemonHost>? {
        guard enabled else { return nil }
        return await WidgetData.read(using: client) { await $0.daemonHost() }
    }
}
