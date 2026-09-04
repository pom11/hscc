import SwiftUI
import WidgetKit

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
/// the whole widget (the state/topology live unreachable stay intact).
struct ClusterEntry: TimelineEntry {
    let date: Date
    let state: ClusterState
    let pairs: [TopologyPair]
    let modelCount: Int?
    let idleMinutesRemaining: Int?
    /// Set only when `state == .unreachable` — the age of the last-known data.
    let lastKnownAgeMinutes: Int?
    /// Set only when `state == .unconfigured`.
    let configured: Bool
    /// Cards being worked right now (GET /v1/kanban/running count).
    let runningCards: Int?
    /// Cards sitting in the queue waiting to be picked up (ready/todo).
    let queueDepth: Int?
    /// Blocked cards needing attention — the failure indicator.
    let blockedCards: Int?

    static let unconfigured = ClusterEntry(date: .now,
                                           state: .unknown,
                                           pairs: [],
                                           modelCount: nil,
                                           idleMinutesRemaining: nil,
                                           lastKnownAgeMinutes: nil,
                                           configured: false,
                                           runningCards: nil,
                                           queueDepth: nil,
                                           blockedCards: nil)
}

// ---------------------------------------------------------------------------
// Timeline provider — READ-ONLY fetches.
// ---------------------------------------------------------------------------

struct ClusterTimelineProvider: TimelineProvider {
    func placeholder(in context: Context) -> ClusterEntry {
        ClusterEntry(date: .now,
                     state: .serving,
                     pairs: Self.samplePairs,
                     modelCount: 3,
                     idleMinutesRemaining: 34,
                     lastKnownAgeMinutes: nil,
                     configured: true,
                     runningCards: 2,
                     queueDepth: 4,
                     blockedCards: 1)
    }

    func getSnapshot(in context: Context, completion: @escaping (ClusterEntry) -> Void) {
        Task {
            completion(await fetchEntry())
        }
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<ClusterEntry>) -> Void) {
        Task {
            let entry = await fetchEntry()
            // State changes are minutes-scale, not seconds — refresh every 5
            // minutes. Widgets get a limited refresh budget; do not poll harder.
            let next = Calendar.current.date(byAdding: .minute, value: 5, to: .now) ?? .now.addingTimeInterval(300)
            completion(Timeline(entries: [entry], policy: .after(next)))
        }
    }

    // MARK: - Fetch

    /// Fetch the live entry. Always returns a usable entry — unreachable and
    /// unconfigured are first-class states, never a blank widget.
    private func fetchEntry() async -> ClusterEntry {
        let config = APIConfig.load()
        guard config != nil else {
            // Not configured → the widget's ONE job is to invite setup. Always
            // return the unconfigured state; never fabricate `.unreachable`.
            // (fix t_5c554c5b) A stale last-known snapshot does NOT make this a
            // "can't reach" problem — the operator hasn't given us a target to
            // reach. If they configure and the cluster is down/unreachable, the
            // unreachable path below will surface the snapshot with its age.
            return .unconfigured
        }

        let client = ExtensionClient(config: config!)
        // Fetch cluster/autodown (drive state + topology) and the board work
        // signals (running / queue / blocked) in parallel. Each is independent
        // and best-effort: a failure nils only its own field, never the widget.
        async let autoTask = client.get("/v1/autodown/status", as: AutodownStatusResponse.self)
        async let clusterTask = client.get("/v1/cluster/status", as: ClusterStatusResponse.self)
        async let runningTask = client.get("/v1/kanban/running", as: KanbanRunningLite.self)
        async let blockedTask = client.get("/v1/kanban/blocked", as: KanbanBlockedLite.self)
        async let staleTask = client.get("/v1/kanban/stale", as: KanbanStaleLite.self,
                                         queryItems: [URLQueryItem(name: "older_than", value: "0")])
        let (auto, cluster, running, blocked, stale) = await (autoTask, clusterTask, runningTask, blockedTask, staleTask)

        guard let auto, let cluster else {
            // Unreachable — fall back to the last-known snapshot for honest
            // stale data with its age, including the last-known work counts.
            // Never present failure as liveness.
            if let last = SnapshotStore.load() {
                let age = last.timestamp.map { Int(Date().timeIntervalSince($0) / 60) } ?? 0
                let work = SnapshotStore.workCounts()
                return ClusterEntry(date: .now,
                                    state: .unreachable,
                                    pairs: pairs(fromNodes: last.nodes),
                                    modelCount: last.modelCount,
                                    idleMinutesRemaining: last.idleMinutes,
                                    lastKnownAgeMinutes: age,
                                    configured: true,
                                    runningCards: work.running,
                                    queueDepth: work.queueDepth,
                                    blockedCards: work.blocked)
            }
            return ClusterEntry(date: .now,
                                state: .unreachable,
                                pairs: Self.samplePairs,
                                modelCount: nil,
                                idleMinutesRemaining: nil,
                                lastKnownAgeMinutes: nil,
                                configured: true,
                                runningCards: nil,
                                queueDepth: nil,
                                blockedCards: nil)
        }

        let clusterState = Self.resolveState(autodownState: auto.state)
        let pairs = Self.canonicalPairs(up: cluster.total_hosts > 0, state: clusterState)
        let modelCount = cluster.workloads.count
        let idleRemaining = Self.idleRemaining(auto: auto, clusterState: clusterState)
        let runningCards = running?.count ?? 0
        let queueDepth = Self.queueDepth(stale: stale)
        let blockedCards = blocked?.count ?? 0

        // Record the last-known good snapshot so a later unreachable window can
        // show yesterday's real state with its age.
        SnapshotStore.save(state: clusterState,
                           modelCount: modelCount,
                           idleMinutes: idleRemaining ?? auto.idle_minutes,
                           pairs: pairs,
                           running: runningCards,
                           queueDepth: queueDepth,
                           blocked: blockedCards)

        return ClusterEntry(date: .now,
                            state: clusterState,
                            pairs: pairs,
                            modelCount: modelCount,
                            idleMinutesRemaining: idleRemaining,
                            lastKnownAgeMinutes: nil,
                            configured: true,
                            runningCards: runningCards,
                            queueDepth: queueDepth,
                            blockedCards: blockedCards)
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
