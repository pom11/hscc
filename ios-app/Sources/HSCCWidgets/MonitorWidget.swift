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

    func color(_ style: WidgetStyle) -> Color {
        switch self {
        case .monitoring: return style.okColor
        case .degraded: return style.warnColor
        case .unreachable, .unconfigured, .paused: return style.secondaryTextColor
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
    /// The per-node cluster monitor snapshot (CPU/RAM/GPU per host), from THIS
    /// fetch. In-memory only and never written to the App Group: `MonitorNode`
    /// carries the node's LAN address, which §7 forbids in shared storage, so
    /// the last-known snapshot below is stored as the `MonitorNodeEntry`
    /// projection instead.
    let nodes: [MonitorNode]
    /// The daemon-host machine data (the machine HSCC runs on).
    let daemonHost: MonitorDaemonHost?
    /// The configured refresh cadence in minutes (0 = Off: no scheduled refresh).
    let refreshMinutes: Int
    /// The content sections this surface renders (from Shared settings). A
    /// section the operator disabled is simply not drawn.
    let showSections: Set<String>
    /// Age of the last-known snapshot in minutes, set only when `.unreachable`.
    let lastKnownAgeMinutes: Int?
    /// How many nodes the persisted last-known projection held, 0 when no
    /// snapshot exists. The unreachable pane needs exactly this fact — "did the
    /// last good fetch have nodes?" — and draws no stale rows, so the wire node
    /// type never has to come back out of the container (§7).
    let lastKnownNodeCount: Int

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
                                           lastKnownAgeMinutes: nil,
                                           lastKnownNodeCount: 0)

    static func paused(_ date: Date = .now) -> MonitorEntry {
        MonitorEntry(date: date,
                     state: .paused,
                     configured: true,
                     paused: true,
                     nodes: [],
                     daemonHost: nil,
                     refreshMinutes: 0,
                     showSections: availableSections,
                     lastKnownAgeMinutes: nil,
                     lastKnownNodeCount: 0)
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
                     lastKnownAgeMinutes: nil,
                     lastKnownNodeCount: 0)
    }

    func getSnapshot(in context: Context, completion: @escaping (MonitorEntry) -> Void) {
        Task { completion(await fetchEntry(family: context.family)) }
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<MonitorEntry>) -> Void) {
        Task {
            let entry = await fetchEntry(family: context.family)
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
    ///
    /// `family` decides whether the daemon-host half is fetched at all: the
    /// Lock Screen accessories draw per-node numbers only, so asking for the
    /// host would spend a request on pixels that cannot exist (§2's budget).
    private func fetchEntry(family: WidgetFamily) async -> MonitorEntry {
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
        // Fetch the per-node monitor metrics and, only for a family that draws
        // it, the daemon-host machine data in parallel. Each is independent and
        // best-effort — a failure degrades only its own section, never the whole
        // widget.
        let wantsHost = family.showsDaemonHost
        async let monitorTask = client.get("/v1/cluster/monitor", as: MonitorResponse.self)
        async let hostTask = Self.daemonHost(client: client, enabled: wantsHost)
        let (monitor, daemonHost) = await (monitorTask, hostTask)

        // Determine what actually reached us. An accessory asked only the node
        // route, so a successful answer there is data even when the fleet listed
        // no hosts; for the Home Screen families the old rule still holds.
        let nodes = monitor?.nodes ?? []
        let hostReached = daemonHost != nil
        let monitorReached = monitor != nil
        let anyData = !nodes.isEmpty || hostReached || (monitorReached && !wantsHost)

        guard anyData else {
            // Neither source reached us → unreachable. Fall back to the
            // last-known snapshot for honest stale data with its age. What comes
            // back out of the container is the projection's node COUNT, never
            // `[MonitorNode]` (§7: the wire type holds the node's LAN address,
            // and the pane below draws no stale rows, so nothing is lost).
            if let last = MonitorSnapshotStore.load() {
                let age = Int(Date().timeIntervalSince1970 - last.date) / 60
                return MonitorEntry(date: .now,
                                    state: .unreachable,
                                    configured: true,
                                    paused: false,
                                    nodes: [],
                                    daemonHost: last.daemonHost,
                                    refreshMinutes: refreshMinutes,
                                    showSections: showSections,
                                    lastKnownAgeMinutes: age,
                                    lastKnownNodeCount: last.nodes.count)
            }
            return MonitorEntry(date: .now,
                                state: .unreachable,
                                configured: true,
                                paused: false,
                                nodes: [],
                                daemonHost: nil,
                                refreshMinutes: refreshMinutes,
                                showSections: showSections,
                                lastKnownAgeMinutes: nil,
                                lastKnownNodeCount: 0)
        }

        // At least one source reached us. Some nodes may have errored — that's
        // honest degradation, not a blank widget.
        let nodeErrors = nodes.filter { $0.error != nil || $0.sample == nil }.count
        let state: MonitorState = nodeErrors > 0 ? .degraded : .monitoring

        // Record the last-known snapshot so a later unreachable window can show
        // this real data with its age. PROJECTED on the way in — the container
        // holds `MonitorNodeEntry`, never the wire node with its address (§7).
        // A family that did not ask for the host keeps the host half already in
        // the snapshot: its own refresh must not erase data a larger family
        // fetched, and a new snapshot date over an old host block would be a lie.
        let snapshotHost = wantsHost ? daemonHost : MonitorSnapshotStore.load()?.daemonHost
        MonitorSnapshotStore.save(nodes: MonitorSnapshotStore.project(nodes),
                                  daemonHost: snapshotHost)

        return MonitorEntry(date: .now,
                            state: state,
                            configured: true,
                            paused: false,
                            nodes: nodes,
                            daemonHost: daemonHost,
                            refreshMinutes: refreshMinutes,
                            showSections: showSections,
                            lastKnownAgeMinutes: nil,
                            lastKnownNodeCount: 0)
    }

    /// The daemon-host read, refused BEFORE a request when the family does not
    /// draw it. Mirrors `ClusterWidget.boardWork`'s `enabled` guard so an
    /// accessory refresh spends nothing on the host half (§2's budget).
    private static func daemonHost(client: ExtensionClient,
                                   enabled: Bool) async -> MonitorDaemonHost? {
        guard enabled else { return nil }
        return await client.get("/v1/daemon/host", as: MonitorDaemonHost.self)
    }

    // MARK: - Sample data (placeholder / first render)

    /// Used for placeholder + first render before any real fetch.
    ///
    /// The three t_56b910c8 sample keys (`cpu_temp_c`, `gpu_temp_c`,
    /// `cpu_load_1m`) and `workloads` are nil here on purpose: this placeholder
    /// feeds the MONITOR widget, which renders CPU/RAM/GPU and slots, and
    /// inventing a temperature for a preview nobody measures is exactly the
    /// "number no route serves" failure. The cluster widget's own placeholder
    /// (`ClusterTimelineProvider.placeholder`) carries real-shaped metric values.
    static let sampleNodes: [MonitorNode] = [
        MonitorNode(host: ".244", error: nil, sample: MonitorSample(
            hostname: "orch-1", uptime_sec: "172800", cpu_usage_pct: "42", mem_used_pct: "61",
            mem_total_mb: "196608", mem_used_mb: "119930", gpu_name: "GB10", gpu_util_pct: "55",
            gpu_mem_used_pct: "47", cpu_temp_c: nil, gpu_temp_c: nil, cpu_load_1m: nil),
            used_slots: 1, free_slots: 1, workloads: nil),
        MonitorNode(host: ".246", error: nil, sample: MonitorSample(
            hostname: "orch-2", uptime_sec: "172800", cpu_usage_pct: "38", mem_used_pct: "57",
            mem_total_mb: "196608", mem_used_mb: "112066", gpu_name: "GB10", gpu_util_pct: "49",
            gpu_mem_used_pct: "44", cpu_temp_c: nil, gpu_temp_c: nil, cpu_load_1m: nil),
            used_slots: 1, free_slots: 1, workloads: nil),
        MonitorNode(host: ".247", error: nil, sample: MonitorSample(
            hostname: "work-1", uptime_sec: "172800", cpu_usage_pct: "78", mem_used_pct: "74",
            mem_total_mb: "196608", mem_used_mb: "145490", gpu_name: "GB10", gpu_util_pct: "88",
            gpu_mem_used_pct: "71", cpu_temp_c: nil, gpu_temp_c: nil, cpu_load_1m: nil),
            used_slots: 2, free_slots: 0, workloads: nil),
        MonitorNode(host: ".248", error: nil, sample: MonitorSample(
            hostname: "work-2", uptime_sec: "172800", cpu_usage_pct: "64", mem_used_pct: "69",
            mem_total_mb: "196608", mem_used_mb: "135659", gpu_name: "GB10", gpu_util_pct: "73",
            gpu_mem_used_pct: "58", cpu_temp_c: nil, gpu_temp_c: nil, cpu_load_1m: nil),
            used_slots: 1, free_slots: 1, workloads: nil),
    ]

    static let sampleDaemonHost = MonitorDaemonHost(
        hostname: "hscc-mac", platform: "macOS", arch: "arm64",
        uptime_seconds: 86400,
        cpu: MonitorDaemonCPU(count: 14, percent: 11.0, load_avg: []),
        memory: MonitorDaemonMemory(total_gb: 36, used_gb: 18, percent: 50),
        disk: MonitorDaemonDisk(total_gb: 456, used_gb: 320, percent: 70),
        processes: 812, daemon_running: true, daemon_uptime_seconds: 3600,
        speak: "Daemon host: macOS arm64 (hscc-mac)")
}
