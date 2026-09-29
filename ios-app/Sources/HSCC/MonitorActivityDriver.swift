import Foundation
import ActivityKit
import BackgroundTasks

// ---------------------------------------------------------------------------
// MonitorActivityDriver — the APP side of the HSCC Monitor Live Activity.
//
// Live Activities are PUSH-ONLY: they cannot fetch data on their own. The app
// is the only process that can call update(). This driver is that feeder:
//
//   * WHILE FOREGROUNDED — a repeating poll loop calls GET /v1/cluster/monitor
//     + GET /v1/daemon/host on the settings cadence (refreshMinutes from the
//     shared App-Group store) and pushes the derived ContentState via update().
//   * WHEN BACKGROUNDED — registers a BGAppRefreshTask so the OS wakes the app
//     on a best-effort schedule (subject to OS scheduling) to push newer data.
//   * HONORS the settings active toggle (only runs when liveActivity.monitor
//     isActive) and showSections (state / nodes / host).
//   * ENDS when settings turn it off. When the app is force-quit in the
//     background the activity freezes at its last pushed state — the pushed
//     staleDate visually ages it, and `sweepLeftoverMonitors` re-hydrates or
//     ends it on next launch (mirroring LiveActivityManager.sweepLeftoverWakes).
//
// Uses the CLASS-based ActivityKit API (`Activity.request`, then instance
// `update` / `end`) — exactly the `LiveActivityManager` pattern.
//
// The monitor decode structs (`ClusterMonitorResponse` / `NodeSample`) already
// live in Models.swift (app target) and are reused here verbatim; only the NEW
// daemon-host wire structs are declared below.
// ---------------------------------------------------------------------------

// MARK: - Wire decoders (pinned against the LIVE shapes)

/// GET /v1/daemon/host — the daemon host machine's OWN metrics.
/// Live-pinned shape (verified 2026-09-28):
///   { hostname, platform, arch, uptime_seconds:int,
///     cpu:{count:int, percent:float, load_avg:[float]},
///     memory:{total_gb, used_gb, percent:float},
///     disk:{total_gb, used_gb, percent:float},
///     processes:int, daemon_running:bool, daemon_uptime_seconds:int }
struct DaemonHostResponse: Decodable {
    let hostname: String?
    let platform: String?   // "macOS"
    let arch: String?       // "arm64"
    let cpu: DaemonCpu?
    let memory: DaemonMem?
    let disk: DaemonDisk?
    let daemon_running: Bool?
}
struct DaemonCpu: Decodable { let percent: Double? }
struct DaemonMem: Decodable { let percent: Double? }
struct DaemonDisk: Decodable { let percent: Double? }

// MARK: - Pure derivation (headless-testable: raw API JSON → ContentState)

/// The pure mapping from raw API responses + settings onto the Live Activity
/// ContentState. No ActivityKit, no Foundation side effects — so a harness can
/// compile and run it verbatim against the real wire shapes.
enum MonitorContent {

    /// Reduce a full IP to the canonical short tail label (".244"), matching
    /// the wake activity's topology vocabulary. Falls back to the raw host on
    /// a non-IP or malformed input.
    static func shortLabel(for host: String) -> String {
        let tail = host.split(separator: ".").last.map(String.init) ?? host
        if tail.allSatisfy({ $0.isNumber }) { return "." + tail }
        return host
    }

    /// Build the ContentState from the decoded monitor + daemon-host responses
    /// plus the section set the surface should render. Node numeric accessors
    /// come from `NodeSample` (already-the-app's model — nil on empty/unreadable,
    /// so an unreadable metric renders honestly as a dash, never a made-up value).
    static func make(monitor: ClusterMonitorResponse?,
                     daemonHost: DaemonHostResponse?,
                     state: String,
                     updatedAt: Date,
                     sections: Set<String>) -> MonitorActivityAttributes.ContentState {
        // Per-node metrics (section "nodes").
        var nodes: [NodeMetric] = []
        if sections.contains("nodes") {
            for node in monitor?.hosts ?? [] {
                guard let sample = node.sample else { continue }
                nodes.append(NodeMetric(label: shortLabel(for: node.host),
                                        cpuPct: sample.cpuUsagePct,
                                        memPct: sample.memUsedPct,
                                        gpuName: sample.gpu_name?.isEmpty == false ? sample.gpu_name : nil,
                                        gpuUtilPct: sample.gpuUtilPct))
            }
        }

        // Daemon-host machine data (section "host").
        var host: HostMetric? = nil
        if sections.contains("host"), let h = daemonHost {
            host = HostMetric(hostname: h.hostname ?? "?",
                              os: h.platform ?? "?",
                              arch: h.arch ?? "?",
                              cpuPct: h.cpu?.percent,
                              memPct: h.memory?.percent,
                              diskPct: h.disk?.percent,
                              daemonRunning: h.daemon_running ?? false)
        }

        return MonitorActivityAttributes.ContentState(state: state,
                                                      nodes: nodes,
                                                      host: host,
                                                      updatedAt: updatedAt)
    }
}

// MARK: - Typed client endpoints for the monitor activity

extension HSCCClient {
    /// GET /v1/cluster/monitor, decoded as the STRONG `ClusterMonitorResponse`
    /// (per-node CPU/RAM/GPU) — distinct from the generic `clusterMonitor()`
    /// (which returns the untyped `ReadResponse` used by FleetView, and cannot
    /// expose typed node metrics).
    func monitorSnapshot() async throws -> ClusterMonitorResponse {
        try await get("/v1/cluster/monitor", as: ClusterMonitorResponse.self)
    }

    /// GET /v1/daemon/host — the daemon host machine's own metrics.
    func daemonHost() async throws -> DaemonHostResponse {
        try await get("/v1/daemon/host", as: DaemonHostResponse.self)
    }
}

// MARK: - The driver

@MainActor
final class MonitorActivityDriver {

    /// The singleton — the app owns exactly one monitor activity feeder.
    static let shared = MonitorActivityDriver()

    /// The live activity's background refresh task identifier (Info.plist).
    static let backgroundRefreshIdentifier = "com.hscc.ios.monitor.backgroundRefresh"

    /// The in-flight monitor activity, if any.
    private(set) var current: Activity<MonitorActivityAttributes>?

    /// Whether the foreground poll loop is running.
    private var pollTask: Task<Void, Never>?

    /// Whether the BGTaskScheduler launch handler has been registered.
    /// BGTaskScheduler requires `register(forTaskWithIdentifier:)` to complete
    /// BEFORE any `submit()` for that identifier — a submit that races ahead of
    /// registration throws the fatal `NSInternalInconsistencyException` ("No
    /// launch handler registered") and terminates the app. `registerBackgroundTasks()`
    /// sets this synchronously (it is called from `didFinishLaunching` before
    /// the scene can activate); `scheduleBackgroundRefresh()` gates on it so a
    /// submit can NEVER precede registration regardless of call site.
    private static var registered = false

    private init() {}

    // MARK: - App lifecycle hooks

    /// Register the background-refresh task once at launch. Idempotent; safe
    /// to call every launch. BGTaskScheduler itself is best-effort — the OS
    /// decides when (or if) the task actually runs, so this is advice, not a
    /// guarantee.
    ///
    /// The `register(forTaskWithIdentifier:)` CALL (which installs the launch
    /// handler) is synchronous and returns as soon as the handler is registered;
    /// the handler *body* runs later on a detached MainActor Task — that
    /// deferral is fine, only the registration call itself must be synchronous
    /// (which this is, and `didFinishLaunching` calls it before the scene can
    /// submit). `registered` flips synchronously so submit is safe from that
    /// point on.
    static func registerBackgroundTasks() {
        BGTaskScheduler.shared.register(
            forTaskWithIdentifier: backgroundRefreshIdentifier,
            using: nil
        ) { task in
            guard let task = task as? BGAppRefreshTask else { return }
            Task { @MainActor in
                // One best-effort push, then reschedule + complete.
                await self.shared.refreshOnce()
                self.shared.scheduleBackgroundRefresh()
                task.setTaskCompleted(success: true)
            }
        }
        // Registration is complete once register() returns — the handler
        // closure runs later, but the launch handler itself is now installed,
        // so submits are safe.
        registered = true
    }

    /// Schedule the next best-effort background refresh. Only submits when the
    /// surface is active; called from launch + after each background push.
    ///
    /// Gates on `registered`: if the launch handler isn't installed yet, a
    /// `submit()` would throw the fatal NSInternalInconsistencyException (the
    /// very crash this fix addresses), so we skip the submit rather than race
    /// it. The handler is registered synchronously in `didFinishLaunching`
    /// (before the scene activates), so by the time any scene-phase submit can
    /// fire the flag is already set — this guard is a belt-and-suspenders
    /// guarantee against ordering regressions at ANY call site.
    func scheduleBackgroundRefresh() {
        guard Self.registered else { return }
        guard setting().isActive else { return }
        let minutes = max(1, setting().refreshMinutes)
        let request = BGAppRefreshTaskRequest(identifier: Self.backgroundRefreshIdentifier)
        request.earliestBeginDate = Date(timeIntervalSinceNow: Double(minutes) * 60)
        try? BGTaskScheduler.shared.submit(request)
    }

    // MARK: - Settings read

    /// The current settings for the monitor Live Activity, bounded, with the
    /// 15-min Live Activity default when the store is absent.
    func setting() -> WidgetSurfaceSettings {
        AppGroupWidgetSettings.load()?.setting(for: .liveActivityMonitor)
            ?? WidgetSurfaceSettings(isActive: true,
                                     refreshMinutes: 15,
                                     showSections: Set(WidgetSurfaceKind.liveActivityMonitor.availableSections))
    }

    // MARK: - Start / stop driven by scene phase + settings

    /// Enter the foreground: if the surface is enabled, re-hydrate any leftover
    /// activity and begin the foreground poll loop. If turned off, ensure
    /// nothing lingers.
    func sceneDidBecomeActive() {
        guard setting().isActive else {
            endAndStopPolling()
            return
        }
        startPolling()
        scheduleBackgroundRefresh()
    }

    /// Leave the foreground: stop the tight poll loop (saving battery) and
    /// schedule a best-effort BGAppRefresh so it keeps updating on OS time.
    func sceneDidEnterBackground() {
        stopPolling()
        scheduleBackgroundRefresh()
    }

    // MARK: - Polling

    /// Start the foreground poll loop. Polls immediately then on the settings
    /// cadence. Callers guard on settings active + scene foreground.
    private func startPolling() {
        guard pollTask == nil else { return }
        let cadence = max(1, setting().refreshMinutes)
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refreshOnce()
                try? await Task.sleep(nanoseconds: UInt64(cadence) * 60_000_000_000)
            }
        }
    }

    private func stopPolling() {
        pollTask?.cancel()
        pollTask = nil
    }

    // MARK: - Push

    /// Poll both endpoints and push the derived content onto the activity.
    /// Lazily starts the activity the first time it has real data to show.
    /// Best-effort: a transport/decode failure just means no newer push this
    /// tick — the previous state stays (and ages via its staleDate).
    func refreshOnce() async {
        // Fetch the client config from the shared store (the app's real
        // connection). If not configured, there is nothing to feed — drop any
        // lingering activity.
        guard let config = APIConfig.load() else {
            endAndStopPolling()
            return
        }
        let client = HSCCClient(host: config.host, port: config.port, token: config.token)

        // Concurrent poll; each failure degrades to nil independently (one
        // route being down must never blank the other section).
        let (monitorResult, daemonResult) = (
            (try? await client.monitorSnapshot()),
            (try? await client.daemonHost()))
        let monitor = monitorResult
        let daemonHost = daemonResult

        // Honest headline state from what the poll actually learned:
        // the API answered + the cluster monitor returned host rows -> serving;
        // otherwise (nothing decodable, or the route degraded to {speak}) ->
        // unreachable. The app's `ClusterMonitorResponse` carries no `success`
        // field (it only has json/speak), so presence of hosts is the signal.
        let hasNodes = !(monitor?.hosts.isEmpty ?? true)
        let hasHost = daemonHost != nil
        let state: String = (hasNodes || hasHost) ? "serving" : "unreachable"

        let content = MonitorContent.make(monitor: monitor,
                                          daemonHost: daemonHost,
                                          state: state,
                                          updatedAt: Date(),
                                          sections: setting().showSections)
        await push(content)
    }

    /// Push content to the activity, lazily starting it on first real data.
    private func push(_ content: MonitorActivityAttributes.ContentState) async {
        if current == nil {
            // Only start a bubble once there is real data (anything other than
            // a bare "unreachable" skeleton). A monitor with no data is better
            // left off than shown as a hollow placeholder.
            guard !(content.nodes.isEmpty && content.host == nil) else { return }
            let attributes = MonitorActivityAttributes()
            do {
                current = try Activity.request(
                    attributes: attributes,
                    content: ActivityContent(state: content, staleDate: staleDate()))
            } catch {
                current = nil
            }
            return
        }
        guard let current else { return }
        await current.update(ActivityContent(state: content, staleDate: staleDate()))
    }

    /// The staleDate for a push: ~3 refresh cycles out, so a frozen bubble
    /// (force-quit) visually ages to "Stale" instead of lying as live, but a
    /// momentarily-hung network isn't immediately greyed.
    private func staleDate() -> Date {
        Date().addingTimeInterval(Double(max(1, setting().refreshMinutes)) * 60 * 3)
    }

    // MARK: - Orphan sweep (mirrors LiveActivityManager.sweepLeftoverWakes)

    /// Re-hydration sweep on app launch: reconcile any leftover monitor activity
    /// left behind by a process kill (force-quit / OS eviction).
    ///
    /// A monitor activity is a CONTINUOUS surface, so unlike the wake (which
    /// should end unless provably in flight) the correct action is:
    ///   * surface still enabled → KEEP it — the driver resumes the poll loop
    ///     and re-pushes live data immediately on the next foreground entry.
    ///   * surface turned off    → END the stale bubble right away.
    static func sweepLeftoverMonitors() {
        let surfaceActive = (AppGroupWidgetSettings.load()?.setting(for: .liveActivityMonitor).isActive) ?? true
        if surfaceActive { return }   // will be resumed by the poll loop
        for activity in Activity<MonitorActivityAttributes>.activities {
            Task { @MainActor in
                await activity.end(nil, dismissalPolicy: .immediate)
            }
        }
    }

    /// End the activity and stop polling (surface turned off / not configured).
    func endAndStopPolling() {
        stopPolling()
        guard let current else { return }
        let activity = current
        self.current = nil
        Task {
            await activity.end(nil, dismissalPolicy: .immediate)
        }
    }
}
