import Foundation
import SwiftUI

// ---------------------------------------------------------------------------
// The menu bar app's ONE data path. Every read goes through the shared
// `WidgetDataClient` (GET-only, App Group config + shared Keychain token,
// multi-address failover), and every displayed number is projected from the
// shared payloads. There is no second HTTP client and no new backend route.
//
// `/v1/cluster/monitor` does not expose a tokens-per-second field in the shared
// model (and the contract keeps unrendered keys undeclared), so the menu bar
// shows the facts the API does expose: fleet state, nodes up, models served,
// running cards, board totals and projects. No invented tok/s number.
// ---------------------------------------------------------------------------

/// The five board counts, summed across every board in `/v1/mobile/summary`.
struct BoardTotals {
    let todo: Int
    let ready: Int
    let running: Int
    let blocked: Int
    let done: Int

    var total: Int { todo + ready + running + blocked + done }

    /// nil when the route degraded (no `boards` key): "no counts" is never a
    /// row of zeros.
    init?(summary: MobileSummaryResponse) {
        guard let boards = summary.boards else { return nil }
        var todo = 0, ready = 0, running = 0, blocked = 0, done = 0
        for board in boards {
            guard let c = board.counts else { continue }
            todo += c.todo
            ready += c.ready
            running += c.running
            blocked += c.blocked
            done += c.done
        }
        self.todo = todo
        self.ready = ready
        self.running = running
        self.blocked = blocked
        self.done = done
    }
}

/// The graphs section's ONE history read (t_be4e1a48). The window default is
/// the durable 1 h tier: `15m` reads the recorder's in-memory 2-second ring,
/// which the deployed API process does not own, so it answers
/// `recorder_ring_absent` with an empty series. 1 h is served from the durable
/// store, and the dropdown still SHOWS the resolution the route reports back,
/// because the same rows mean different charts at 1 m and 5 m. The route is the
/// canonical `GET /v1/cluster/history` (DECISION-t_447cc7fc); the retired
/// `/v1/metrics/history` is never read from this app.
enum DashboardGraphPlan {
    /// `1h` — tier 1's 1-minute roll-ups, the shallowest window the deployed
    /// route can serve. A `ClusterGraphWindow` rather than a string so the
    /// request, the axis tick format and the shared captions all read the same
    /// value; the fetch sends `.requestValue`.
    static let window: ClusterGraphWindow = .oneHour

    /// Per-node temperature and utilisation — the card's ask, in ONE request.
    /// None of these is a `*_max` column, so all four stay requestable at
    /// every tier.
    static let metrics: [ClusterHistoryMetric] = [.cpuTempC, .gpuTempC, .gpuUtilPct, .cpuUtilPct]
}

/// One node's drawn history: the shared chart layer's panel (its lines, each
/// carrying the true current and the window's true min/max) and the unit groups
/// those lines fold into for the marks. Both come from `ClusterGraphReading` at
/// the width the dropdown actually draws at — this app derives no series, no
/// min/max, no unit group and no tier of its own.
struct DashboardGraphNode: Identifiable {
    let panel: ClusterGraphPanel
    let minis: [ClusterGraphMini]

    var id: String { panel.host }
}

/// The graphs section's view of one `GET /v1/cluster/history` answer. Series
/// building and projection go through the shared chart layer
/// (`ClusterGraphReading.panels`, which calls `ClusterHistorySeriesSet.make`) —
/// this file keeps the route's stated window and step next to the panels so the
/// dropdown can show what the points mean.
struct DashboardHistory {
    let window: String
    let resolutionS: Int
    /// The window the axis prints its ticks for: the PAYLOAD's own window when
    /// the route named one this app recognises (`since`/`until` reads answer with
    /// a derived span label), otherwise what this app asked for.
    let axisWindow: ClusterGraphWindow
    let nodes: [DashboardGraphNode]
    /// Why there is nothing to draw, in whoever already knew's words.
    let emptyLine: String

    init(response: ClusterHistoryResponse) {
        window = response.window
        resolutionS = response.resolution_s
        axisWindow = ClusterGraphWindow(rawValue: response.window) ?? DashboardGraphPlan.window
        let panels = ClusterGraphReading.panels(response: response,
                                                metrics: DashboardGraphPlan.metrics,
                                                pixelWidth: DashboardGraphLayout.miniPixelWidth)
        nodes = panels.map {
            DashboardGraphNode(panel: $0, minis: ClusterGraphReading.minis(from: $0.lines))
        }
        emptyLine = Self.reason(for: response, axisWindow: axisWindow)
    }

    /// The payload's own sentence when it sent one; otherwise the shared
    /// layer's reason for the lead metric, which separates "the `metrics=` filter
    /// named no field of this series" from "nothing is stored at this tier" from
    /// "the 2 s ring lives in the daemon process". A gap with a cause, never a
    /// quiet fleet.
    private static func reason(for response: ClusterHistoryResponse,
                               axisWindow: ClusterGraphWindow) -> String {
        if let note = response.note ?? response.error, !note.isEmpty { return note }
        let lead = DashboardGraphPlan.metrics[0]
        return ClusterGraphCaption.emptyReason(for: ClusterHistorySeriesSet.make(response, for: lead),
                                               window: axisWindow)
    }
}

/// Everything the menu bar label and the dropdown draw from one refresh.
struct DashboardSnapshot {
    var configured = true
    var clusterState: ClusterState = .unknown
    var nodesUp: Int?
    var nodesTotal: Int?
    var modelCount: Int?
    var running: CardsRunningProjection?
    var boardTotals: BoardTotals?
    var projectCount: Int?
    var projects: [ProjectLite] = []
    /// The main orchestrator's unacknowledged alerts ("Needs you", card R3).
    /// nil when the read failed; empty is the honest "nothing needs you".
    var needsMe: NeedsMeFeed?
    /// Why the alert read failed, already reduced to the shared client's
    /// address-free vocabulary. nil on a successful read.
    var needsMeFailure: String?
    /// The local merge queue's rows ("Merge queue", card MQ5). nil when the
    /// read failed; empty is the honest "nothing queued".
    var mergeQueue: MergeQueueFeed?
    /// Why the merge-queue read failed, in the shared client's vocabulary.
    var mergeQueueFailure: String?
    /// Per-node history for the graphs section — the ONE `/v1/cluster/history`
    /// read this app makes, projected through the shared chart layer.
    var history: DashboardHistory?
    var lastRefresh: Date?
    /// The first honest failure from this refresh, already reduced to the
    /// shared client's address-free `shortLabel`/`operatorLine` vocabulary.
    var failure: String?

    // MARK: The two CLI surfaces (t_f5fdc6b2)

    /// The raw `/v1/cluster/monitor` answer. Kept because `used_slots` and
    /// `free_slots` live on the host objects and the CLI view-model's rows
    /// deliberately do not carry them; every DISPLAYED string still comes from
    /// the projections below, never from this.
    var monitorResponse: MonitorResponse?
    /// `hscc cluster monitor`'s host + serving tables as the dropdown draws
    /// them. nil means there was no table to draw (the fleet is down, or the
    /// read failed) and `fleetEmptyText` says which.
    var fleetTile: DashboardFleetReading.Tile?
    /// What to print in place of the monitor table when there is none: the
    /// route's own `speak` line for a degraded answer, the read's `shortLabel`
    /// for a failed one. Never a table of dashes.
    var fleetEmptyText: String?
    /// The fleet numbers the dense menu-bar title draws.
    var fleetStats: MenuBarFleet.Stats = .noAnswer
    /// `hscc cluster status`'s workload table + host tally + slot pair. nil when
    /// the read failed or the route answered with its own `error` — a degraded
    /// route's `total_hosts: 0` is never drawn as an empty cluster.
    var statusPanel: ClusterStatusReading.Panel?
    /// Why the status panel is not there, when it is not.
    var statusError: String?

    /// The optional short stat next to the menu bar icon. nil draws the icon
    /// alone — never a fabricated zero for a read that did not answer.
    func menuBarText(for stat: MenuBarStat) -> String? {
        switch stat {
        case .off:
            return nil
        case .runningCards:
            guard let running else { return nil }
            let count = running.runningCount ?? (running.rows.isEmpty ? nil : running.rows.count)
            guard let count else { return nil }
            return "\(count) run"
        case .clusterNodes:
            guard let nodesUp, let nodesTotal else { return nil }
            return "\(nodesUp)/\(nodesTotal)"
        case .fleetGpu:
            return fleetStats.gpuLoadTitle()
        case .hottestTemp:
            return fleetStats.hottestTitle()
        }
    }

    /// The VoiceOver sentence for the same stat: the host, sensor and coverage
    /// the dense title had to leave out.
    func menuBarAccessibilityText(for stat: MenuBarStat) -> String? {
        switch stat {
        case .fleetGpu:
            return fleetStats.gpuLoadAccessibilityText()
        case .hottestTemp:
            return fleetStats.hottestAccessibilityText()
        case .off, .runningCards, .clusterNodes:
            return nil
        }
    }
}

@MainActor
final class DashboardMonitor: ObservableObject {
    @Published private(set) var snapshot = DashboardSnapshot()
    @Published private(set) var isRefreshing = false

    init() {
        // Refreshes immediately, then on the operator's cadence. The loop lives
        // for the process lifetime; the menu bar label is always mounted, so
        // the stat stays live even while the dropdown is closed.
        Task { [weak self] in
            await self?.runLoop()
        }
    }

    private func runLoop() async {
        while !Task.isCancelled {
            await refresh()
            let seconds = MacPreferences.storedRefreshSeconds()
            try? await Task.sleep(nanoseconds: UInt64(seconds) * 1_000_000_000)
        }
    }

    func refresh() async {
        guard !isRefreshing else { return }
        isRefreshing = true
        defer { isRefreshing = false }

        guard let client = WidgetDataClient.makeIfConfigured() else {
            var fresh = DashboardSnapshot()
            fresh.configured = false
            fresh.lastRefresh = Date()
            snapshot = fresh
            return
        }

        var next = snapshot
        next.configured = true
        var failures: [String] = []
        var anySuccess = false

        switch await client.autodownStatus() {
        case .success(let auto):
            next.clusterState = Self.clusterState(from: auto.state)
            anySuccess = true
        case .failure(let failure):
            next.clusterState = .unreachable
            failures.append(failure.shortLabel)
        }

        switch await client.clusterMonitor() {
        case .success(let monitor):
            let tally = ClusterNodeTally.project(monitor)
            next.nodesUp = tally.nodesUp
            next.nodesTotal = tally.nodesTotal
            if let hosts = monitor.json?.hosts, !hosts.isEmpty {
                let ids = Set(hosts.flatMap { $0.workloads ?? [] }.compactMap { $0.cluster_id })
                next.modelCount = ids.count
            } else {
                next.modelCount = nil
            }
            // The dropdown's two CLI tables and the title's fleet numbers, both
            // projected from the same payload the tally came from.
            let view = ClusterMonitorViewModel.project(monitor)
            next.monitorResponse = monitor
            next.fleetTile = DashboardFleetReading.tile(from: view, response: monitor)
            // A degraded `{speak}`-only body has no table: show the route's own
            // sentence (`fleet monitor unavailable`), not a table of dashes.
            next.fleetEmptyText = monitor.speak
            next.fleetStats = MenuBarFleet.Stats.project(view)
            anySuccess = true
        case .failure(let failure):
            failures.append(failure.shortLabel)
            next.fleetEmptyText = failure.shortLabel
        }

        // `hscc cluster status` — the workloads table, the host tally, and the
        // slot pair the monitor route carries. Unlike the reads above this one
        // DOES clear on failure: a workload table is exactly the kind of list an
        // operator would otherwise read as "these are the engines running now".
        switch await client.clusterStatus() {
        case .success(let status):
            next.statusPanel = ClusterStatusReading.panel(status: status,
                                                          hosts: next.monitorResponse?.json?.hosts)
            next.statusError = status.error
            anySuccess = true
        case .failure(let failure):
            next.statusPanel = nil
            next.statusError = failure.shortLabel
            failures.append(failure.shortLabel)
        }

        // The graphs section's series: ONE request for all nodes and all four
        // planned metrics (canonical /v1/cluster/history — DECISION-t_447cc7fc),
        // projected by the shared layer. An empty answer is kept, reason and
        // all; a failed read keeps the last good history rather than claiming
        // zero, like every other stat in this loop.
        switch await client.clusterHistory(window: DashboardGraphPlan.window.requestValue,
                                           metrics: DashboardGraphPlan.metrics.map(\.rawValue)) {
        case .success(let response):
            next.history = DashboardHistory(response: response)
            anySuccess = true
        case .failure(let failure):
            failures.append(failure.shortLabel)
        }

        switch await client.kanbanRunning() {
        case .success(let running):
            next.running = CardsRunningProjection(running: running, blocked: nil, now: Date())
            anySuccess = true
        case .failure(let failure):
            failures.append(failure.shortLabel)
        }

        switch await client.mobileSummary() {
        case .success(let summary):
            next.boardTotals = BoardTotals(summary: summary)
            anySuccess = true
        case .failure(let failure):
            failures.append(failure.shortLabel)
        }

        switch await client.projects() {
        case .success(let projects):
            next.projectCount = projects.count ?? projects.projects.count
            next.projects = projects.rows(limit: 6)
            anySuccess = true
        case .failure(let failure):
            failures.append(failure.shortLabel)
        }

        // The alert feed is the one surface that still answers "what needs the
        // operator"; it is a read-only GET, not a notification registration.
        switch await client.alerts(limit: 100) {
        case .success(let payload):
            next.needsMe = NeedsMeFeed.from(payload)
            next.needsMeFailure = nil
            anySuccess = true
        case .failure(let failure):
            next.needsMe = nil
            next.needsMeFailure = failure.shortLabel
            failures.append(failure.shortLabel)
        }

        // The local merge queue is read-only: the daemon owns the queue, and
        // this dropdown only asks where each PR sits.
        switch await client.mergeQueue() {
        case .success(let payload):
            next.mergeQueue = MergeQueueFeed.from(payload)
            next.mergeQueueFailure = nil
            anySuccess = true
        case .failure(let failure):
            next.mergeQueue = nil
            next.mergeQueueFailure = failure.shortLabel
            failures.append(failure.shortLabel)
        }

        if anySuccess { next.lastRefresh = Date() }
        next.failure = failures.first
        snapshot = next
    }

    /// The same `up` / `waking` / `down` vocabulary the cluster widget maps.
    private static func clusterState(from raw: String?) -> ClusterState {
        switch raw?.lowercased() {
        case "up", "serving": return .serving
        case "waking": return .waking
        case "down": return .down
        default: return .unknown
        }
    }
}
