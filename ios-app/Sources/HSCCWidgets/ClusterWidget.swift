import SwiftUI
import WidgetKit

// ---------------------------------------------------------------------------
// Timeline provider — READ-ONLY fetches.
//
// Two requests per refresh for every family (the §2 budget), chosen by size:
//   GET /v1/cluster/monitor  — the host table + serving table (the rows the
//                              CLI prints, projected by `FleetProjection`).
//                              Every family pays for this one.
//   GET /v1/cluster/status   — the workload/`total_hosts` count the other CLI
//                              command prints, used as the jobs line. Every
//                              family EXCEPT `systemSmall`.
//   GET /v1/cluster/history  — the recorder's stored series behind the small
//                              tile's GPU% sparklines (t_2681d3de). Only
//                              `systemSmall`, in the slot the others spend on
//                              `status` — the tile draws no jobs line, so the
//                              swap costs that size nothing.
//
// A failed read nils only its own half. Nothing is invented and no cached
// number is drawn as live: a stale entry says "updated Nm ago", a failed one
// says why. A failed HISTORY read nils only the trends — the rows it cannot
// feed are live from the monitor read, and the cells fall back to their bars.
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
            let refresh = AppGroupWidgetSettings.load()?.setting(for: .widgetCluster).refreshMinutes ?? 5
            guard refresh > 0 else {
                completion(Timeline(entries: [entry], policy: .atEnd))
                return
            }
            let next = Calendar.current.date(byAdding: .minute, value: refresh, to: .now)
                ?? .now.addingTimeInterval(300)
            completion(Timeline(entries: [entry], policy: .after(next)))
        }
    }

    // MARK: - Fetch

    private func fetchEntry(family: WidgetFamily) async -> ClusterEntry {
        let surface = AppGroupWidgetSettings.load()?.setting(for: .widgetCluster)
        if let surface, !surface.isActive { return .paused() }
        let showSections = surface?.showSections ?? ClusterEntry.availableSections

        guard let client = WidgetDataClient.makeIfConfigured() else {
            return .unconfigured
        }

        async let monitorTask = WidgetData.read(using: client) { await $0.clusterMonitor() }
        async let statusTask = Self.statusRead(client: client, enabled: family.showsStatus)
        async let historyTask = Self.historyRead(client: client, enabled: family.wantsHostHistory)
        let (monitorOutcome, statusOutcome, historyOutcome) = await (monitorTask, statusTask,
                                                                     historyTask)

        let fleet = monitorOutcome.value.flatMap(FleetProjection.init(projecting:))
        // The same decoded response, projected by the CLI view-model. No third
        // request: one read, two projections, so the tally above the table and
        // the table itself can never come from different refreshes.
        let monitor = monitorOutcome.value.flatMap { ClusterMonitorViewModel.project($0) }
        let status = statusOutcome?.value.flatMap(ClusterStatusSummary.init)
        // The series behind the sparklines; nil (read skipped, failed, or
        // empty) simply means every GPU cell draws its single-sample bar. It
        // is not an entry failure — the tile is not waiting on it.
        let history = historyOutcome?.value
        let failure = monitorOutcome.failure ?? statusOutcome?.failure

        // A live monitor read is the only writer of the last-good timestamp.
        // A half-degraded refresh must not overwrite it.
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
            if case .stale(_, let age, _) = monitorOutcome {
                return age
            }
            guard let at = SnapshotStore.lastKnownTimestamp() else { return nil }
            return WidgetData.ageMinutes(from: at)
        }()

        return ClusterEntry(date: .now,
                            fleet: fleet,
                            monitor: monitor,
                            status: status,
                            failure: failure,
                            configured: true,
                            paused: false,
                            lastKnownAgeMinutes: age,
                            history: history,
                            showSections: showSections)
    }

    /// Dataset 5 (`GET /v1/cluster/status`), paid for by every family except
    /// the small tile. `enabled == false` sends ZERO requests (§2's per-family
    /// budget is held by construction, not by discipline).
    private static func statusRead(client: WidgetDataClient,
                                   enabled: Bool) async -> WidgetDataOutcome<ClusterStatusResponse>? {
        guard enabled else { return nil }
        return await WidgetData.read(using: client) { await $0.clusterStatus() }
    }

    /// Dataset 5 (`GET /v1/cluster/history`), paid for by the small tile
    /// alone: the window and metric come from `ClusterSmallGrid`'s own
    /// constants, so the read and the code drawing it cannot drift apart.
    private static func historyRead(client: WidgetDataClient,
                                    enabled: Bool) async -> WidgetDataOutcome<ClusterHistoryResponse>? {
        guard enabled else { return nil }
        return await WidgetData.read(using: client) {
            await $0.clusterHistory(window: ClusterSmallGrid.trendWindow,
                                    metrics: [ClusterSmallGrid.trendMetric.rawValue])
        }
    }
}
