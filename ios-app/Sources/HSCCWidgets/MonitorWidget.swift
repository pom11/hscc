import SwiftUI
import WidgetKit

// ---------------------------------------------------------------------------
// HSCC Monitor widget data model.
// ---------------------------------------------------------------------------

/// The headline state the Monitor widget surface. Honest states, each mapped
/// to the Theme.Semantic role that matches: green when live, amber when
/// partially degraded (some nodes failed to sample), neutral when the API is
/// unreachable / unconfigured / paused.
enum MonitorState {
    /// Live monitor data fetched and rendered.
    case monitoring
    /// Fetched, but some nodes errored (partial / degraded snapshot).
    case degraded
    /// The API can't be reached.
    case unreachable
    /// No host/port/token configured yet.
    case unconfigured
    /// The operator paused this surface in Settings.
    case paused

    var color: Color {
        switch self {
        case .monitoring: return Theme.Semantic.ok
        case .degraded: return Theme.Semantic.warn
        case .unreachable, .unconfigured, .paused: return Theme.Semantic.neutral
        }
    }

    /// The headline copy for the state line.
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

/// A single Monitor widget snapshot. Follows `ClusterEntry`'s honesty pattern:
/// `.unconfigured`, `.unreachable`, and `.paused` are first-class states — the
/// widget is never blank. The sections the operator toggled OFF in Shared
/// settings are simply not rendered by the view (the data is still fetched).
struct MonitorEntry: TimelineEntry {
    let date: Date
    /// The headline state.
    let state: MonitorState
    /// Whether the operator has configured host/port/token.
    let configured: Bool
    /// True when the operator paused this surface in Settings.
    let paused: Bool
    /// The per-node cluster monitor snapshot (CPU/RAM/GPU per host).
    let nodes: [MonitorNode]
    /// The daemon-host machine data (the machine HSCC runs on).
    let daemonHost: DaemonHostResponse?
    /// The configured refresh cadence in minutes (0 = Off: no scheduled refresh).
    let refreshMinutes: Int
    /// The content sections this surface renders (from Shared settings). A
    /// section the operator disabled is simply not drawn.
    let showSections: Set<String>
    /// Age of the last-known snapshot in minutes, set only when `.unreachable`.
    let lastKnownAgeMinutes: Int?

    /// The sections this surface knows about (drives fallback defaults).
    static let availableSections = WidgetSurfaceKind.widgetMonitor.availableSections

    static let unconfigured = MonitorEntry(date: .now,
                                           state: .unconfigured,
                                           configured: false,
                                           paused: false,
                                           nodes: [],
                                           daemonHost: nil,
                                           refreshMinutes: 5,
                                           showSections: availableSections,
                                           lastKnownAgeMinutes: nil)

    static func paused(_ date: Date = .now) -> MonitorEntry {
        MonitorEntry(date: date,
                     state: .paused,
                     configured: true,
                     paused: true,
                     nodes: [],
                     daemonHost: nil,
                     refreshMinutes: 0,
                     showSections: availableSections,
                     lastKnownAgeMinutes: nil)
    }
}

// ---------------------------------------------------------------------------
// Timeline provider — READ-ONLY fetches.
// ---------------------------------------------------------------------------

struct MonitorTimelineProvider: TimelineProvider {
    func placeholder(in context: Context) -> MonitorEntry {
        MonitorEntry(date: .now,
                     state: .monitoring,
                     configured: true,
                     paused: false,
                     nodes: Self.sampleNodes,
                     daemonHost: Self.sampleDaemonHost,
                     refreshMinutes: 5,
                     showSections: MonitorEntry.availableSections,
                     lastKnownAgeMinutes: nil)
    }

    func getSnapshot(in context: Context, completion: @escaping (MonitorEntry) -> Void) {
        Task { completion(await fetchEntry()) }
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<MonitorEntry>) -> Void) {
        Task {
            let entry = await fetchEntry()
            // Refresh on the operator's configured cadence (t_dea36c48),
            // defaulting to 5 minutes. 0 = Off: no scheduled refresh — let the
            // OS refresh at its own discretion rather than a tight loop.
            let refresh = entry.refreshMinutes
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

    /// Fetch the live entry. Always returns a usable entry — unreachable and
    /// unconfigured are first-class states, never a blank widget.
    private func fetchEntry() async -> MonitorEntry {
        // Honor the shared "Active" toggle (t_dea36c48). Default ON when the
        // store or this surface's entry is absent, so the widget keeps
        // rendering as it did before the settings existed.
        let surface = AppGroupWidgetSettings.load()?.setting(for: .widgetMonitor)
        let refreshMinutes = surface?.refreshMinutes ?? 5
        let showSections = surface?.showSections ?? MonitorEntry.availableSections
        if let surface, !surface.isActive {
            return .paused()
        }

        let config = APIConfig.load()
        guard config != nil else {
            // Not configured → the widget's ONE job is to invite setup. Never
            // fabricate `.unreachable` (mirrors ClusterWidget t_5c554c5b): the
            // operator hasn't given us a target to reach yet.
            return .unconfigured
        }

        let client = ExtensionClient(config: config!)
        // Fetch the per-node monitor metrics and the daemon-host machine data in
        // parallel. Each is independent and best-effort — a failure degrades
        // only its own section, never the whole widget.
        async let monitorTask = client.get("/v1/cluster/monitor", as: MonitorResponse.self)
        async let hostTask = client.get("/v1/daemon/host", as: DaemonHostResponse.self)
        let (monitor, daemonHost) = await (monitorTask, hostTask)

        // Determine what actually reached us.
        let nodes = monitor?.nodes ?? []
        let hostReached = daemonHost != nil
        let anyData = !nodes.isEmpty || hostReached

        guard anyData else {
            // Neither source returned data → unreachable. Fall back to the
            // last-known snapshot for honest stale data with its age.
            if let last = MonitorSnapshotStore.load() {
                let age = Int(Date().timeIntervalSince1970 - last.date) / 60
                return MonitorEntry(date: .now,
                                    state: .unreachable,
                                    configured: true,
                                    paused: false,
                                    nodes: last.nodes,
                                    daemonHost: last.daemonHost,
                                    refreshMinutes: refreshMinutes,
                                    showSections: showSections,
                                    lastKnownAgeMinutes: age)
            }
            return MonitorEntry(date: .now,
                                state: .unreachable,
                                configured: true,
                                paused: false,
                                nodes: [],
                                daemonHost: nil,
                                refreshMinutes: refreshMinutes,
                                showSections: showSections,
                                lastKnownAgeMinutes: nil)
        }

        // At least one source reached us. Some nodes may have errored — that's
        // honest degradation, not a blank widget.
        let nodeErrors = nodes.filter { $0.error != nil || $0.sample == nil }.count
        let state: MonitorState = nodeErrors > 0 ? .degraded : .monitoring

        // Record the last-known snapshot so a later unreachable window can show
        // this real data with its age.
        MonitorSnapshotStore.save(nodes: nodes, daemonHost: daemonHost)

        return MonitorEntry(date: .now,
                            state: state,
                            configured: true,
                            paused: false,
                            nodes: nodes,
                            daemonHost: daemonHost,
                            refreshMinutes: refreshMinutes,
                            showSections: showSections,
                            lastKnownAgeMinutes: nil)
    }

    // MARK: - Sample data (placeholder / first render)

    /// Used for placeholder + first render before any real fetch.
    static let sampleNodes: [MonitorNode] = [
        MonitorNode(host: ".244", error: nil, sample: MonitorSample(
            hostname: "orch-1", uptime_sec: "172800", cpu_usage_pct: "42", mem_used_pct: "61",
            mem_total_mb: "196608", mem_used_mb: "119930", gpu_name: "GB10", gpu_util_pct: "55",
            gpu_mem_used_pct: "47"), used_slots: 1, free_slots: 1),
        MonitorNode(host: ".246", error: nil, sample: MonitorSample(
            hostname: "orch-2", uptime_sec: "172800", cpu_usage_pct: "38", mem_used_pct: "57",
            mem_total_mb: "196608", mem_used_mb: "112066", gpu_name: "GB10", gpu_util_pct: "49",
            gpu_mem_used_pct: "44"), used_slots: 1, free_slots: 1),
        MonitorNode(host: ".247", error: nil, sample: MonitorSample(
            hostname: "work-1", uptime_sec: "172800", cpu_usage_pct: "78", mem_used_pct: "74",
            mem_total_mb: "196608", mem_used_mb: "145490", gpu_name: "GB10", gpu_util_pct: "88",
            gpu_mem_used_pct: "71"), used_slots: 2, free_slots: 0),
        MonitorNode(host: ".248", error: nil, sample: MonitorSample(
            hostname: "work-2", uptime_sec: "172800", cpu_usage_pct: "64", mem_used_pct: "69",
            mem_total_mb: "196608", mem_used_mb: "135659", gpu_name: "GB10", gpu_util_pct: "73",
            gpu_mem_used_pct: "58"), used_slots: 1, free_slots: 1),
    ]

    static let sampleDaemonHost = DaemonHostResponse(
        hostname: "hscc-mac", platform: "macOS", arch: "arm64",
        uptime_seconds: 86400,
        cpu: DaemonHostCPU(count: 14, percent: 11.0, load_avg: []),
        memory: DaemonHostMemory(total_gb: 36, used_gb: 18, percent: 50),
        disk: DaemonHostDisk(total_gb: 456, used_gb: 320, percent: 70),
        processes: 812, daemon_running: true, daemon_uptime_seconds: 3600,
        speak: "Daemon host: macOS arm64 (hscc-mac)")
}
