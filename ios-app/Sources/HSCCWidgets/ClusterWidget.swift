import SwiftUI
import WidgetKit

// ---------------------------------------------------------------------------
// Cluster widget data model.
// ---------------------------------------------------------------------------

/// A single widget snapshot. `.unreachable` carries the last-known state and
/// its age so the widget is never blank or stale-looking-live. `.unconfigured`
/// is the honest state when the operator hasn't set host/port/token yet.
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

    static let unconfigured = ClusterEntry(date: .now,
                                           state: .unknown,
                                           pairs: [],
                                           modelCount: nil,
                                           idleMinutesRemaining: nil,
                                           lastKnownAgeMinutes: nil,
                                           configured: false)
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
                     configured: true)
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
            // Not configured — but still surface any last-known snapshot the
            // app recorded so a reconnect shows real history, not a blank.
            if let last = SnapshotStore.load() {
                let age = last.timestamp.map { Int(Date().timeIntervalSince($0) / 60) } ?? 0
                return ClusterEntry(date: .now,
                                    state: .unreachable,
                                    pairs: pairs(fromNodes: last.nodes),
                                    modelCount: last.modelCount,
                                    idleMinutesRemaining: last.idleMinutes,
                                    lastKnownAgeMinutes: age,
                                    configured: false)
            }
            return .unconfigured
        }

        let client = ExtensionClient(config: config!)
        // Fetch autodown status (drives state + idle) and cluster status
        // (drives topology + model count) in parallel.
        async let autoTask = client.get("/v1/autodown/status", as: AutodownStatusResponse.self)
        async let clusterTask = client.get("/v1/cluster/status", as: ClusterStatusResponse.self)
        let (auto, cluster) = await (autoTask, clusterTask)

        guard let auto, let cluster else {
            // Unreachable — fall back to the last-known snapshot for honest
            // stale data with its age. Never present failure as liveness.
            if let last = SnapshotStore.load() {
                let age = last.timestamp.map { Int(Date().timeIntervalSince($0) / 60) } ?? 0
                return ClusterEntry(date: .now,
                                    state: .unreachable,
                                    pairs: pairs(fromNodes: last.nodes),
                                    modelCount: last.modelCount,
                                    idleMinutesRemaining: last.idleMinutes,
                                    lastKnownAgeMinutes: age,
                                    configured: true)
            }
            return ClusterEntry(date: .now,
                                state: .unreachable,
                                pairs: Self.samplePairs,
                                modelCount: nil,
                                idleMinutesRemaining: nil,
                                lastKnownAgeMinutes: nil,
                                configured: true)
        }

        let clusterState = Self.resolveState(autodownState: auto.state)
        let pairs = Self.canonicalPairs(up: cluster.total_hosts > 0, state: clusterState)
        let modelCount = cluster.workloads.count
        let idleRemaining = Self.idleRemaining(auto: auto, clusterState: clusterState)

        // Record the last-known good snapshot so a later unreachable window can
        // show yesterday's real state with its age.
        SnapshotStore.save(state: clusterState,
                           modelCount: modelCount,
                           idleMinutes: idleRemaining ?? auto.idle_minutes,
                           pairs: pairs)

        return ClusterEntry(date: .now,
                            state: clusterState,
                            pairs: pairs,
                            modelCount: modelCount,
                            idleMinutesRemaining: idleRemaining,
                            lastKnownAgeMinutes: nil,
                            configured: true)
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
