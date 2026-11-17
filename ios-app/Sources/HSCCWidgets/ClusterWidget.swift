import SwiftUI
import WidgetKit

// ---------------------------------------------------------------------------
// Timeline provider — READ-ONLY fetches.
// ---------------------------------------------------------------------------

struct ClusterTimelineProvider: TimelineProvider {
    func placeholder(in context: Context) -> ClusterEntry {
        ClusterEntry(date: .now,
                     state: .serving,
                     pairs: Self.samplePairs,
                     modelCount: 2,
                     idleMinutesRemaining: 34,
                     lastKnownAgeMinutes: nil,
                     configured: true,
                     runningCards: 2,
                     queueDepth: 4,
                     blockedCards: 1,
                     paused: false,
                     nodesUp: 4,
                     nodesTotal: 4,
                     gpuUtilPct: 88,
                     gpuTempC: 63,
                     cpuTempC: 71,
                     load1m: 2.7,
                     failure: nil,
                     showSections: ClusterEntry.availableSections)
    }

    func getSnapshot(in context: Context, completion: @escaping (ClusterEntry) -> Void) {
        Task {
            completion(await fetchEntry(family: context.family))
        }
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<ClusterEntry>) -> Void) {
        Task {
            let entry = await fetchEntry(family: context.family)
            // State changes are minutes-scale, not seconds — refresh on the
            // operator's configured cadence (t_dea36c48), defaulting to 5
            // minutes. Widgets get a limited refresh budget; do not poll harder.
            let refresh = AppGroupWidgetSettings.load()?.setting(for: .widgetCluster).refreshMinutes ?? 5
            // 0 = Off: no scheduled refresh. Let the OS refresh at its own
            // discretion rather than scheduling an instant tight loop.
            guard refresh > 0 else {
                completion(Timeline(entries: [entry], policy: .atEnd))
                return
            }
            let next = Calendar.current.date(byAdding: .minute, value: refresh, to: .now) ?? .now.addingTimeInterval(300)
            completion(Timeline(entries: [entry], policy: .after(next)))
        }
    }

    // MARK: - Fetch

    /// Fetch the live entry. Always returns a usable entry — unreachable and
    /// unconfigured are first-class states, never a blank widget.
    ///
    /// `family` decides whether the board row is fetched at all: the accessory
    /// families draw no board row, so asking for it would spend three of the
    /// surface's request budget on pixels that cannot exist (§2).
    private func fetchEntry(family: WidgetFamily) async -> ClusterEntry {
        // Honor the shared "Active" toggle (t_dea36c48). Default ON when the
        // store or this surface's entry is absent, so the widget keeps
        // rendering exactly as it did before the settings existed. When the
        // operator explicitly pauses it, render an honest paused state rather
        // than live data (the widget cannot remove itself programmatically).
        let surface = AppGroupWidgetSettings.load()?.setting(for: .widgetCluster)
        if let surface, !surface.isActive {
            return .paused()
        }
        let showSections = surface?.showSections ?? ClusterEntry.availableSections

        // Not configured -> the widget's ONE job is to invite setup. A nil
        // client IS that signal (`WidgetDataClient.makeIfConfigured` checks the
        // same App-Group config and shared token every other surface reads).
        // Always return the unconfigured state; never fabricate `.unreachable`
        // (fix t_5c554c5b): a stale last-known snapshot does NOT make this a
        // "can't reach" problem — the operator hasn't given us a target to
        // reach. If they configure and the cluster is down/unreachable, the
        // unreachable path below will surface the snapshot with its age.
        let client = WidgetDataClient.makeIfConfigured()
        guard let client else { return .unconfigured }

        // The two dataset-5 reads, in parallel: the state line and the fleet
        // metrics are independent, and each half degrades on its own.
        async let autoTask = WidgetData.read(using: client) { await $0.autodownStatus() }
        async let monitorTask = WidgetData.read(using: client) { await $0.clusterMonitor() }
        // The board row is dataset 3, fetched only when it will be rendered.
        let wantsBoard = family.showsBoardWork && showSections.contains("board")
        async let boardTask = Self.boardWork(client: client, enabled: wantsBoard)
        let (autoOutcome, monitorOutcome, board) = await (autoTask, monitorTask, boardTask)

        // Projected inside the same statement, so nothing downstream touches the
        // raw 10.7 KB payload: `output` (54% of the body) is not even declared
        // on `MonitorResponse`, and the projection keeps counts only (§7).
        let fleet: ClusterFleet? = monitorOutcome.value.flatMap(ClusterFleet.init(projecting:))
        // The failure that took this tile's live data, if any. `.notConfigured`
        // cannot arrive here (a nil client returned above), so anything left is
        // the cluster's or the transport's.
        let failure = autoOutcome.failure ?? monitorOutcome.failure

        // The headline state comes from autodown ALONE — which is the point of
        // the t_56b910c8 fix: a fleet that answers "down" renders Down, with the
        // node counts it actually reported (or none), instead of "Can't reach
        // the cluster" because a second read could not be decoded.
        let state: ClusterState
        switch autoOutcome {
        case .live(let auto, _), .stale(let auto, _, _):
            state = Self.resolveState(autodownState: auto.state)
        case .failed:
            state = .unreachable
        }

        // Last-known half. `showsStaleData` is the ONE fallback rule
        // (`WidgetDataClient`): a connectivity/token/decode failure may keep the
        // snapshot with its age; a "we were never told where to look" failure
        // may not.
        let snapshot = SnapshotStore.load()
        let staleAge: Int? = {
            guard let failure, failure.showsStaleData, let timestamp = snapshot?.timestamp else { return nil }
            return WidgetData.ageMinutes(from: timestamp)
        }()

        // Nodes up/down. `state == .down` with no host list is still honestly
        // `0 up` — a down fleet has no up nodes; that is the state, not an
        // invented measurement. The denominator comes from the monitor, and
        // otherwise from the last-known topology so the fraction is not a lone
        // zero pretending to be a count.
        let nodesUp: Int?
        let nodesTotal: Int?
        if let fleet, let reported = fleet.nodesUp, let total = fleet.nodesTotal {
            nodesUp = reported
            nodesTotal = total
        } else if state == .down {
            nodesUp = 0
            nodesTotal = snapshot?.nodes.count
        } else {
            nodesUp = nil
            nodesTotal = nil
        }

        // Models served, from this read when it answered; otherwise the
        // last-known count with its age (the store keeps that one).
        let modelCount = fleet?.modelsServed ?? (staleAge != nil ? snapshot?.modelCount : nil)

        // The topology strip is a STATE glyph (it has always been derived from
        // the fleet state, not per-host data) — the per-host truth is the node
        // count and the metric line. Only an unreachable tile with a stored
        // strip shows stored dots, and only those are `pairsAreStale`.
        let pairs: [TopologyPair]
        let pairsAreStale: Bool
        if state == .unreachable, let snapshot, !snapshot.nodes.isEmpty {
            pairs = self.pairs(fromNodes: snapshot.nodes)
            pairsAreStale = true
        } else {
            pairs = Self.canonicalPairs(up: (nodesUp ?? 0) > 0 || state == .serving, state: state)
            pairsAreStale = false
        }

        let idleRemaining: Int?
        switch autoOutcome {
        case .live(let auto, _), .stale(let auto, _, _):
            idleRemaining = Self.idleRemaining(auto: auto, clusterState: state)
        case .failed:
            idleRemaining = nil
        }

        // Record the last-known good snapshot so a later unreachable window can
        // show yesterday's real state with its age. Only a fully live read
        // writes it — a half-degraded tile must not overwrite good data.
        if case .live = autoOutcome, let fleet, fleet.nodesTotal != nil {
            SnapshotStore.save(state: state,
                               modelCount: fleet.modelsServed,
                               idleMinutes: idleRemaining ?? Self.idleMinutes(of: autoOutcome),
                               pairs: pairs,
                               running: board?.running,
                               queueDepth: board?.queueDepth,
                               blocked: board?.blocked)
        }

        return ClusterEntry(date: .now,
                            state: state,
                            pairs: pairs,
                            modelCount: modelCount,
                            idleMinutesRemaining: idleRemaining,
                            lastKnownAgeMinutes: staleAge,
                            configured: true,
                            runningCards: board?.running,
                            queueDepth: board?.queueDepth,
                            blockedCards: board?.blocked,
                            paused: false,
                            nodesUp: nodesUp,
                            nodesTotal: nodesTotal,
                            gpuUtilPct: fleet?.gpuUtilPct,
                            gpuTempC: fleet?.gpuTempC,
                            cpuTempC: fleet?.cpuTempC,
                            load1m: fleet?.load1m,
                            failure: failure,
                            showSections: showSections,
                            pairsAreStale: pairsAreStale)
    }

    // MARK: - Board work (dataset 3, conditional)

    /// The kanban trio §5.3 sanctions, run in parallel, returning nil counts on
    /// failure rather than zeros. `enabled` is checked BEFORE any request, so a
    /// family or an operator that does not render the board row pays nothing.
    private static func boardWork(client: WidgetDataClient, enabled: Bool) async -> BoardWork? {
        guard enabled else { return nil }
        async let runningTask = client.kanbanRunning()
        async let blockedTask = client.kanbanBlocked()
        async let staleTask = client.kanbanStale(olderThanDays: 0)
        let (running, blocked, stale) = await (runningTask, blockedTask, staleTask)
        return BoardWork(running: succeeded(running)?.count,
                         queueDepth: queueDepth(stale: succeeded(stale)),
                         blocked: succeeded(blocked)?.count)
    }

    /// The value of a typed read, or nil when it failed. `Result` has no `.value`
    /// and `try? read.get()` would swallow the failure this surface must be able
    /// to tell apart — but for the board row a nil simply omits the metric, so
    /// the failure kind is not needed here (the cluster half keeps it).
    private static func succeeded<T>(_ result: Result<T, WidgetDataFailure>) -> T? {
        if case .success(let value) = result { return value }
        return nil
    }

    // MARK: - State + topology derivation (honest)

    /// Map the autodown `state` string to the cluster state enum.
    private static func resolveState(autodownState: String?) -> ClusterState {
        switch autodownState {
        case "up": return .serving
        case "waking": return .waking
        case "down": return .down
        default: return .unknown
        }
    }

    /// The canonical two TP pairs. `up` drives whether the pair is serving;
    /// `state` colours a waking fleet amber vs a down fleet red vs serving mint.
    private static func canonicalPairs(up: Bool, state: ClusterState) -> [TopologyPair] {
        let nodeState: TopologyNode.NodeState
        switch state {
        case .serving: nodeState = up ? .up : .down
        case .waking: nodeState = .busy
        case .down: nodeState = .down
        case .unreachable, .unknown: nodeState = .unknown
        }
        let orchestrator = TopologyPair(
            nodes: [
                TopologyNode(label: ".244", state: nodeState),
                TopologyNode(label: ".246", state: nodeState),
            ],
            role: "orchestrator"
        )
        let worker = TopologyPair(
            nodes: [
                TopologyNode(label: ".247", state: nodeState),
                TopologyNode(label: ".248", state: nodeState),
            ],
            role: "worker"
        )
        return [orchestrator, worker]
    }

    /// The idle-limit arm of `autodown/status`, for the snapshot only (the
    /// remaining-minutes figure is derived, this is what the route reported).
    private static func idleMinutes(of outcome: WidgetDataOutcome<AutodownStatusResponse>) -> Int? {
        switch outcome {
        case .live(let auto, _), .stale(let auto, _, _): return auto.idle_minutes
        case .failed: return nil
        }
    }

    /// Idle-minutes remaining before autodown fires, if armed and serving.
    /// Computed from the real signals the API gives (idle limit + last
    /// activity) — NOT faked from a timer.
    private static func idleRemaining(auto: AutodownStatusResponse, clusterState: ClusterState) -> Int? {
        guard auto.enabled == true, clusterState == .serving,
              let limit = auto.idle_minutes, limit > 0 else { return nil }
        // last_activity_iso is when work last happened; autodown fires
        // `idle_minutes` after that. If it's absent we can't compute remaining.
        // The API emits fractional seconds, so use `.withFractionalSeconds`
        // (the default ISO8601DateFormatter returns nil on that format).
        guard let iso = auto.last_activity_iso else { return nil }
        let isoFmt = ISO8601DateFormatter()
        isoFmt.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        guard let date = isoFmt.date(from: iso) else { return nil }
        let elapsed = Date().timeIntervalSince(date) / 60
        let remaining = Int(limit) - Int(elapsed)
        return max(remaining, 0)
    }

    /// Board queue depth — cards sitting in the queue waiting to be picked up.
    ///
    /// Derived from /v1/kanban/stale (all non-terminal cards with per-card
    /// `status`): queue depth = cards whose status is `ready` or `todo` — work
    /// the dispatcher has not yet started. Skips the running/claimed cards (in
    /// progress) and any other status. Returns nil when the stale list is
    /// absent, so a failed fetch omits the metric instead of showing a false 0.
    private static func queueDepth(stale: KanbanStaleLite?) -> Int? {
        guard let tasks = stale?.tasks else { return nil }
        return tasks.reduce(0) { acc, card in
            guard let s = card.status?.lowercased() else { return acc }
            return (s == "ready" || s == "todo") ? acc + 1 : acc
        }
    }

    private func pairs(fromNodes nodes: [TopologyNode]) -> [TopologyPair] {
        // Re-group the flat 4-node list back into the two canonical pairs.
        guard nodes.count >= 4 else {
            return Self.canonicalPairs(up: false, state: .unknown)
        }
        return [
            TopologyPair(nodes: Array(nodes[0...1]), role: "orchestrator"),
            TopologyPair(nodes: Array(nodes[2...3]), role: "worker"),
        ]
    }

    /// Used for placeholder + first-render before any real fetch.
    static let samplePairs: [TopologyPair] = canonicalPairs(up: true, state: .serving)
}
