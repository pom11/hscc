import SwiftUI
import WidgetKit

// ---------------------------------------------------------------------------
// Cluster widget provider — READ-ONLY fetches.
//
// The 2026-11-20 C1 layout reads:
//   GET /v1/cluster/monitor  — nodes, temperatures, power, serving tok/s.
//   GET /v1/cluster/history  — the large tile's 2 h total-throughput chart.
//                              The route serves presets; 6h is cut to its
//                              last 2 h of points client-side.
//   GET /v1/daemon/host      — the large tile's daemon-state footer.
//
// The last two are paid for only by the sizes that draw them. A failed history
// or daemon read leaves that block out; only a failed monitor read can make the
// surface stale/offline, because that is the read every C1 number comes from.
// ---------------------------------------------------------------------------

struct ClusterTimelineProvider: TimelineProvider {
    func placeholder(in context: Context) -> ClusterEntry {
        ClusterEntry.placeholder
    }

    func getSnapshot(in context: Context, completion: @escaping (ClusterEntry) -> Void) {
        Task { completion(await fetchEntry(family: context.family)) }
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<ClusterEntry>) -> Void) {
        Task {
            let entry = await fetchEntry(family: context.family)
            let refresh = entry.refreshMinutes
            guard refresh > 0 else {
                completion(Timeline(entries: [entry], policy: .atEnd))
                return
            }
            let next = Calendar.current.date(byAdding: .minute, value: refresh, to: .now)
                ?? .now.addingTimeInterval(Double(refresh) * 60)
            completion(Timeline(entries: [entry], policy: .after(next)))
        }
    }

    private func fetchEntry(family: WidgetFamily) async -> ClusterEntry {
        let surface = AppGroupWidgetSettings.load()?.setting(for: .widgetCluster)
        if let surface, !surface.isActive { return .paused() }
        let refresh = surface?.refreshMinutes ?? 15
        let showSections = surface?.showSections ?? ClusterEntry.availableSections

        guard let client = WidgetDataClient.makeIfConfigured() else {
            return .unconfigured
        }

        let wantsHistory = family == .systemLarge || family == .systemExtraLarge
        async let monitorTask = WidgetData.read(using: client) { await $0.clusterMonitor() }
        async let historyTask = Self.optionalHistory(client: client, enabled: wantsHistory)
        async let daemonTask = Self.optionalDaemonHost(client: client, enabled: wantsHistory)
        let (monitorOutcome, historyOutcome, daemonOutcome) = await (monitorTask, historyTask,
                                                                     daemonTask)

        let fleet = monitorOutcome.value.flatMap(FleetProjection.init(projecting:))
        let monitor = monitorOutcome.value.flatMap { ClusterMonitorViewModel.project($0) }
        let tableData = ClusterMonitorTables.project(monitorOutcome.value)
        let status: ClusterStatusSummary? = nil
        let history = historyOutcome?.value
        let failure = Self.failure(of: monitorOutcome)
        let lockScreen = CLILockScreenMetrics(monitorOutcome.value)
        let dataAt = Self.dataDate(of: monitorOutcome)

        if case .live = monitorOutcome, let fleet {
            SnapshotStore.save(state: fleet.tally.nodesUp == fleet.tally.nodesTotal ? .serving : .waking,
                               modelCount: fleet.serving.models,
                               idleMinutes: nil,
                               pairs: [],
                               running: nil,
                               queueDepth: nil,
                               blocked: nil)
        }

        let age: Int? = {
            guard let failure, failure.showsStaleData else { return nil }
            if case .stale(_, let age, _) = monitorOutcome { return age }
            guard let at = SnapshotStore.lastKnownTimestamp() else { return nil }
            return WidgetData.ageMinutes(from: at)
        }()

        return ClusterEntry(date: .now,
                            fleet: fleet,
                            monitor: monitor,
                            status: status,
                            tableData: tableData,
                            failure: failure,
                            configured: true,
                            paused: false,
                            lastKnownAgeMinutes: age,
                            showSections: showSections,
                            lockScreen: lockScreen,
                            history: history,
                            dataAt: dataAt,
                            refreshMinutes: refresh,
                            daemonHost: daemonOutcome?.value,
                            throughput: ClusterThroughput.series(history))
    }

    private static func optionalHistory(client: WidgetDataClient,
                                        enabled: Bool) async -> WidgetDataOutcome<ClusterHistoryResponse>? {
        guard enabled else { return nil }
        return await WidgetData.read(using: client) {
            await $0.clusterHistory(window: "6h", metrics: [ClusterHistoryMetric.genTps.rawValue])
        }
    }

    private static func optionalDaemonHost(client: WidgetDataClient,
                                           enabled: Bool) async -> WidgetDataOutcome<MonitorDaemonHost>? {
        guard enabled else { return nil }
        return await WidgetData.read(using: client) { await $0.daemonHost() }
    }

    private static func failure(of outcome: WidgetDataOutcome<MonitorResponse>) -> WidgetDataFailure? {
        if let failure = outcome.failure { return failure }
        if case .stale(_, _, let reason) = outcome { return reason }
        return nil
    }

    private static func dataDate(of outcome: WidgetDataOutcome<MonitorResponse>) -> Date? {
        switch outcome {
        case .live(let value, let fetchedAt):
            if let raw = value.json?.timestamp,
               raw > 1_000_000_000, raw < Date().timeIntervalSince1970 + 86_400 {
                return Date(timeIntervalSince1970: raw)
            }
            return fetchedAt
        case .stale(_, let age, _):
            return Date().addingTimeInterval(-Double(age) * 60)
        case .failed:
            return nil
        }
    }
}
