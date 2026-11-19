import SwiftUI
import WidgetKit

// ---------------------------------------------------------------------------
// Timeline provider — READ-ONLY fetches.
//
// Two requests, the §2 budget:
//   GET /v1/cluster/monitor  — the host table + serving table (the rows the
//                              CLI prints, projected by `FleetProjection`).
//   GET /v1/cluster/status   — the workload/`total_hosts` count the other CLI
//                              command prints, used as the jobs line.
//
// A failed read nils only its own half. Nothing is invented and no cached
// number is drawn as live: a stale entry says "updated Nm ago", a failed one
// says why.
// ---------------------------------------------------------------------------

struct ClusterTimelineProvider: TimelineProvider {
    func placeholder(in context: Context) -> ClusterEntry {
        ClusterEntry.placeholder
    }

    func getSnapshot(in context: Context, completion: @escaping (ClusterEntry) -> Void) {
        Task { completion(await fetchEntry()) }
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<ClusterEntry>) -> Void) {
        Task {
            let entry = await fetchEntry()
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

    private func fetchEntry() async -> ClusterEntry {
        let surface = AppGroupWidgetSettings.load()?.setting(for: .widgetCluster)
        if let surface, !surface.isActive { return .paused() }
        let showSections = surface?.showSections ?? ClusterEntry.availableSections

        guard let client = WidgetDataClient.makeIfConfigured() else {
            return .unconfigured
        }

        async let monitorTask = WidgetData.read(using: client) { await $0.clusterMonitor() }
        async let statusTask = WidgetData.read(using: client) { await $0.clusterStatus() }
        let (monitorOutcome, statusOutcome) = await (monitorTask, statusTask)

        let fleet = monitorOutcome.value.flatMap(FleetProjection.init(projecting:))
        // The same decoded response, projected by the CLI view-model. No third
        // request: one read, two projections, so the tally above the table and
        // the table itself can never come from different refreshes.
        let monitor = monitorOutcome.value.flatMap { ClusterMonitorViewModel.project($0) }
        let status = statusOutcome.value.flatMap(ClusterStatusSummary.init)
        let failure = monitorOutcome.failure ?? statusOutcome.failure
        // The Lock Screen's four numbers come from the SAME monitor body, through
        // the CLI view-model (t_61ed3f5e) — one read, so a Lock Screen number and
        // the CLI's cell for that host cannot disagree, and a failed read nils
        // this half exactly like `fleet` does rather than inventing zeroes.
        let lockScreen = CLILockScreenMetrics(monitorOutcome.value)

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
                            showSections: showSections,
                            lockScreen: lockScreen)
    }
}
