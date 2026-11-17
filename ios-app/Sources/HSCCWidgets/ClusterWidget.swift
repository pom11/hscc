import SwiftUI
import WidgetKit

// ---------------------------------------------------------------------------
// Cluster widget — data model, projection and timeline provider.
//
// The views live in `ClusterWidgetViews.swift` (same file set): five families
// from one entry — systemSmall, systemMedium and the three Lock Screen
// accessories. This file owns what is FETCHED and how a wire payload becomes
// the handful of numbers a widget is allowed to draw (WIDGET_DATA_CONTRACT §2's
// budget, §5.5's field list, §7's redaction).
//
// Reads (t_56b910c8): dataset 5 is served by two routes, and this surface uses
// exactly those two per refresh — §2's "≤ 2 requests per surface per refresh":
//
//   * `GET /v1/autodown/status` — the fleet power state (`up` / `waking` /
//     `down`), which is the headline, plus the idle minutes before autodown
//     fires.
//   * `GET /v1/cluster/monitor` — every host the monitor tried to sample, with
//     `error` / `sample` / `workloads`, which answers nodes up/down, models
//     served and GPU / temperature / load in the SAME body.
//
// `GET /v1/cluster/status` is deliberately NOT read here any more. §4.4
// measured it answering 39 B `{"speak": "cluster status unavailable"}` when the
// fleet is powered off, and `ClusterStatusResponse` declares `workloads` /
// `total_hosts` as REQUIRED — so on a down fleet that read threw a decode
// failure, the provider's `guard let auto, let cluster` failed, and the widget
// rendered "Can't reach the cluster" for a cluster the API had just told us was
// DOWN. Everything that read contributed (a model count, a host count) is
// carried by `/v1/cluster/monitor`, which degrades to a decodable
// `{"speak": ...}` instead (§4.4, same measurement).
//
// The board work row (running / queue / blocked) is dataset 3, not this
// dataset, and it stays — but it is now CONDITIONAL: the accessory families
// render no board row (`WidgetFamily.showsBoardWork`) and the operator can turn
// the section off in Settings, so those three reads are skipped entirely. An
// accessory refresh is therefore 2 requests, and the board row's own budget is
// the kanban trio §5.3 sanctions.
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// The projection: /v1/cluster/monitor -> the numbers this surface draws.
// ---------------------------------------------------------------------------

/// What the cluster monitor says about the FLEET, projected to seven numbers
/// (t_56b910c8). This is the only place the monitor payload is interpreted, so
/// the "which aggregate and why" decisions are in one readable place and are
/// checkable headless by `scripts/cluster_widget_check.sh`.
///
/// Every field is nil-able and nil MEANS "this body did not report it" — never
/// zero. §6 rule 1's discipline (a missing key is a bug, zero is a value)
/// applies to a projection too: rendering an unmeasured metric as `0` tells the
/// operator the GPU is idle when the truth is that the node was not sampled.
struct ClusterFleet: Equatable {
    /// Hosts the monitor reported with no `error` — they answered.
    let nodesUp: Int?
    /// Hosts the monitor listed at all (errored ones included: the route lists
    /// what it tried to sample, so this is the denominator, not a guess).
    let nodesTotal: Int?
    /// DISTINCT `cluster_id`s across the hosts. A tensor-parallel model holds
    /// both hosts of a pair under one id, so summing per-host workload counts
    /// would double-count it (4 hosts / 2 models in the committed fixture).
    let modelsServed: Int?
    /// Busiest GPU utilisation across the hosts, in percent.
    let gpuUtilPct: Double?
    /// Hottest GPU, in °C.
    let gpuTempC: Double?
    /// Hottest CPU, in °C.
    let cpuTempC: Double?
    /// Highest 1-minute load average across the hosts.
    let load1m: Double?

    /// Project the wire payload.
    ///
    /// nil when the route DEGRADED (`json` absent — §4.4's 38 B down-state
    /// answer): "no data" must stay distinguishable from "four hosts, none up".
    init?(projecting response: MonitorResponse) {
        guard let payload = response.json else { return nil }
        let hosts = payload.hosts
        guard !hosts.isEmpty else {
            self.nodesUp = nil
            self.nodesTotal = nil
            self.modelsServed = nil
            self.gpuUtilPct = nil
            self.gpuTempC = nil
            self.cpuTempC = nil
            self.load1m = nil
            return
        }
        self.nodesTotal = hosts.count
        self.nodesUp = hosts.filter { $0.isUp }.count
        let ids = Set(hosts.flatMap { $0.workloads ?? [] }.compactMap { $0.cluster_id })
        self.modelsServed = ids.count
        // MAX, not mean: one saturated node is the fact an operator acts on, and
        // an average of four hosts hides it (a 100% GPU on a 4-node fleet reads
        // 25% mean). Each metric is nil when NO host reported it.
        self.gpuUtilPct = Self.highest(hosts.compactMap { $0.sample?.gpuUtilPct })
        self.gpuTempC = Self.highest(hosts.compactMap { $0.sample?.gpuTempC })
        self.cpuTempC = Self.highest(hosts.compactMap { $0.sample?.cpuTempC })
        self.load1m = Self.highest(hosts.compactMap { $0.sample?.cpuLoad1m })
    }

    /// The maximum of the reported values, nil when nothing was reported.
    private static func highest(_ values: [Double]) -> Double? {
        values.max()
    }

    /// Whether this projection carries anything a metric line could draw.
    var hasMetrics: Bool {
        gpuUtilPct != nil || gpuTempC != nil || cpuTempC != nil || load1m != nil
    }
}

/// How hot a percentage or temperature reads, in the app's OWN display bands.
///
/// NOT a hardware spec: no route serves a thermal limit for these boxes, so the
/// widget never claims one — it colours, and it always prints the number with
/// its unit next to the colour. The bands are set above every value measured in
/// the committed 4-node fixture (63 °C GPU, 70.7 °C CPU, GPU util 0-88%) so the
/// normal state of a working fleet reads calm, and the utilisation bands match
/// `MonitorMeter.color(for:isCPU:)` exactly (CPU >85 warn / >95 bad, other
/// gauges >90 / >95) — a number and the monitor widget's bar for the same
/// metric can never disagree about what "hot" means.
enum ClusterMetricHeat: Equatable {
    case calm
    case warm
    case hot
    /// Nothing reported — render an em dash, never a colour.
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

    /// Load has NO band on purpose: a load average is only hot relative to the
    /// core count, and no route serves the per-node core count (only the
    /// daemon-host sidecar carries `cpu.count`). Any band here would be an
    /// invented number, so load is printed uncoloured.
    static var load: ClusterMetricHeat { .unknown }
}

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
/// the whole widget (the state/topology live unreachable stay intact). A FAILED
/// read nils its count; it is never rendered as `0`.
///
/// The cluster signals added in t_56b910c8 (`nodesUp` / `nodesTotal` /
/// `gpuUtilPct` / `gpuTempC` / `cpuTempC` / `load1m`) follow the same rule and
/// are LIVE-ONLY: `SnapshotStore` does not persist a metric projection, so a
/// failed monitor read omits the metric line rather than showing a stale one
/// next to a live state line. `modelCount` does have a last-known value, so it
/// survives with `lastKnownAgeMinutes`.
struct ClusterEntry: TimelineEntry {
    let date: Date
    let state: ClusterState
    let pairs: [TopologyPair]
    let modelCount: Int?
    let idleMinutesRemaining: Int?
    /// Set when ANY part of this tile is last-known rather than live — the age
    /// of that data. Its presence is also the view's signal to dim the
    /// topology, so a stale strip is never mistaken for a live one.
    let lastKnownAgeMinutes: Int?
    /// Set only when `state == .unconfigured`.
    let configured: Bool
    /// Cards being worked right now (GET /v1/kanban/running count).
    let runningCards: Int?
    /// Cards sitting in the queue waiting to be picked up (ready/todo).
    let queueDepth: Int?
    /// Blocked cards needing attention — the failure indicator.
    let blockedCards: Int?
    /// True when the operator paused this surface in Settings (t_dea36c48).
    /// The widget still exists (a widget can't be removed programmatically) but
    /// renders an honest "paused in Settings" state instead of live data.
    let paused: Bool
    /// Hosts the monitor reported as up, nil when the monitor gave no host list.
    let nodesUp: Int?
    /// Hosts the monitor listed (errored included) — the denominator of
    /// `nodesUp`, nil when unknown. Never 0 unless the route really listed none.
    let nodesTotal: Int?
    /// Busiest GPU utilisation (percent), hottest GPU and CPU (°C), highest
    /// 1-minute load — all nil when the monitor did not report them.
    let gpuUtilPct: Double?
    let gpuTempC: Double?
    let cpuTempC: Double?
    let load1m: Double?
    /// The read that removed this tile's live data, when one did. The accessory
    /// families render `failure.shortLabel`; the system families render
    /// `.operatorLine`. nil while the tile is live.
    let failure: WidgetDataFailure?
    /// The content sections this surface renders (from Shared settings). A
    /// section the operator disabled is simply not drawn.
    let showSections: Set<String>
    /// True when the topology strip is the last-known one from the store rather
    /// than derived from this refresh. The strip is normally a STATE glyph, so
    /// a live state with a stale model count must not dim it — dimming means
    /// "these dots are old data", which is only true when they came from storage.
    let pairsAreStale: Bool

    /// Explicit init rather than the synthesized memberwise one, so the
    /// pre-existing factories (`unconfigured`, `paused`) — which predate
    /// t_56b910c8 — keep compiling: the new signals default HERE. They cannot
    /// default at the declaration, because a `let` with a declaration default is
    /// already initialized and cannot be assigned from an initializer.
    init(date: Date,
         state: ClusterState,
         pairs: [TopologyPair],
         modelCount: Int?,
         idleMinutesRemaining: Int?,
         lastKnownAgeMinutes: Int?,
         configured: Bool,
         runningCards: Int?,
         queueDepth: Int?,
         blockedCards: Int?,
         paused: Bool,
         nodesUp: Int? = nil,
         nodesTotal: Int? = nil,
         gpuUtilPct: Double? = nil,
         gpuTempC: Double? = nil,
         cpuTempC: Double? = nil,
         load1m: Double? = nil,
         failure: WidgetDataFailure? = nil,
         showSections: Set<String> = WidgetSurfaceKind.widgetCluster.availableSections,
         pairsAreStale: Bool = false) {
        self.date = date
        self.state = state
        self.pairs = pairs
        self.modelCount = modelCount
        self.idleMinutesRemaining = idleMinutesRemaining
        self.lastKnownAgeMinutes = lastKnownAgeMinutes
        self.configured = configured
        self.runningCards = runningCards
        self.queueDepth = queueDepth
        self.blockedCards = blockedCards
        self.paused = paused
        self.nodesUp = nodesUp
        self.nodesTotal = nodesTotal
        self.gpuUtilPct = gpuUtilPct
        self.gpuTempC = gpuTempC
        self.cpuTempC = cpuTempC
        self.load1m = load1m
        self.failure = failure
        self.showSections = showSections
        self.pairsAreStale = pairsAreStale
    }

    /// The sections this surface knows about (drives fallback defaults).
    static let availableSections = WidgetSurfaceKind.widgetCluster.availableSections

    /// True when something here is last-known rather than live.
    var isStale: Bool { lastKnownAgeMinutes != nil }

    /// The one short string an accessory renders when it has NOTHING live to
    /// draw. The accessory views try real data first (the node gauge, the
    /// models count, the metric line) and fall back here, so a partly-degraded
    /// tile keeps showing what it does know.
    var shortGateLabel: String {
        if !configured { return "Set up in HSCC" }
        if paused { return "Paused" }
        return failure?.shortLabel ?? "No data"
    }

    static let unconfigured = ClusterEntry(date: .now,
                                           state: .unknown,
                                           pairs: [],
                                           modelCount: nil,
                                           idleMinutesRemaining: nil,
                                           lastKnownAgeMinutes: nil,
                                           configured: false,
                                           runningCards: nil,
                                           queueDepth: nil,
                                           blockedCards: nil,
                                           paused: false)

    static func paused(_ date: Date = .now) -> ClusterEntry {
        ClusterEntry(date: date,
                     state: .unknown,
                     pairs: [],
                     modelCount: nil,
                     idleMinutesRemaining: nil,
                     lastKnownAgeMinutes: nil,
                     configured: true,
                     runningCards: nil,
                     queueDepth: nil,
                     blockedCards: nil,
                     paused: true)
    }
}

/// The board work row's three counts (dataset 3). All optional, independently:
/// a kanban read that fails nils its own count and never zeroes another's.
struct BoardWork: Equatable {
    let running: Int?
    let queueDepth: Int?
    let blocked: Int?
}

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

// ---------------------------------------------------------------------------
// Which families render the board row.
// ---------------------------------------------------------------------------

extension WidgetFamily {
    /// True for the families that draw the board work row. The Lock Screen
    /// accessories have no room for it (one line, one gauge, two lines), so the
    /// provider must not spend three kanban reads on them (§2's budget).
    /// `systemExtraLarge` and anything newer keeps the row — the default is the
    /// system family side, and a new size getting the row is the safe error.
    var showsBoardWork: Bool {
        switch self {
        case .accessoryInline, .accessoryCircular, .accessoryRectangular, .accessoryCorner:
            return false
        default:
            return true
        }
    }
}
