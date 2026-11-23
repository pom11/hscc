import Foundation
import ActivityKit
import BackgroundTasks
import WidgetKit

// ---------------------------------------------------------------------------
// ClusterHealthActivityDriver — the APP side of "Cluster watch" (A2), the one
// surviving Live Activity.
//
// The activity is OPERATOR-STARTED from the app's cluster surface and runs for
// at most eight hours (`ClusterHealthActivityAttributes.maxRuntime`). It is
// never started by a foreground transition: `sceneDidBecomeActive` only adopts
// an activity the operator already started (and resumes its loop), so removing
// the other four activities cannot leave an auto-started poll behind.
//
// WHAT IT READS, and nothing else (docs/WIDGET_DATA_CONTRACT.md §3 dataset 5):
//
//   * `GET /v1/autodown/status`  → the serving state and autodown armed state.
//   * `GET /v1/cluster/monitor`  → the node tally, per-host load cells and the
//     serving model / run / wait / tok/s totals, via the SAME projections the
//     widget tiles run.
//   * `GET /v1/daemon/host`      → the daemon state the A2 footer prints.
//
// All three go through `WidgetData.read(using:lastKnown:)`, the app's one
// stale-while-error rule. A stale read keeps the snapshot's REAL time — the
// push's `updatedAt` is the data's date, never `Date()` — and the pushed
// `staleDate` is derived from that date, so a failed refresh cannot present
// last-known data as fresh.
//
// OFF means OFF. A zero `refreshMinutes` stops the foreground loop and the
// BGAppRefresh submit instead of turning into a one-minute poll (the audit's
// W3 finding). Only this driver's poll loop exists; the removed activities'
// loops and BG identifiers went with their files.
// ---------------------------------------------------------------------------

@MainActor
final class ClusterHealthActivityDriver {

    /// The singleton — the app owns exactly one Cluster watch feeder.
    static let shared = ClusterHealthActivityDriver()

    /// The activity's background-refresh task identifier (Info.plist's
    /// `BGTaskSchedulerPermittedIdentifiers`, which `project.yml` generates).
    static let backgroundRefreshIdentifier = "com.hscc.ios.clusterhealth.backgroundRefresh"

    /// The in-flight activity, if any.
    private(set) var current: Activity<ClusterHealthActivityAttributes>?

    /// The foreground poll loop, while it is running.
    private var pollTask: Task<Void, Never>?

    /// When the operator started this watch. Adopted from the attributes when
    /// the process resumes an existing bubble.
    private var startedAt: Date?

    /// Whether the BGTaskScheduler launch handler is installed. `submit()` for
    /// an identifier with no handler throws the fatal
    /// `NSInternalInconsistencyException` and takes the app with it, so every
    /// submit is gated on this.
    private static var registered = false

    /// Last-known payloads, for stale-while-error. In-process only: the App
    /// Group never sees a wire payload (§7).
    private var lastAutodown: (value: AutodownStatusResponse, at: Date)?
    private var lastMonitor: (value: MonitorResponse, at: Date)?
    private var lastDaemon: (value: MonitorDaemonHost, at: Date)?

    private init() {}

    /// Whether an operator-started watch is on screen.
    var isRunning: Bool { current != nil }

    // MARK: - App lifecycle hooks

    /// Register the background-refresh handler once at launch, from
    /// `didFinishLaunching`, BEFORE any scene can submit. Idempotent.
    static func registerBackgroundTasks() {
        BGTaskScheduler.shared.register(
            forTaskWithIdentifier: backgroundRefreshIdentifier,
            using: nil
        ) { task in
            guard let task = task as? BGAppRefreshTask else { return }
            Task { @MainActor in
                await self.shared.refreshOnce()
                self.shared.scheduleBackgroundRefresh()
                task.setTaskCompleted(success: true)
            }
        }
        registered = true
    }

    /// Arm the next best-effort background wake. Only while an operator-started
    /// activity is actually up, the surface is active, the cadence is not Off,
    /// and the eight-hour budget has not run out.
    func scheduleBackgroundRefresh() {
        guard Self.registered else { return }
        guard setting().isActive, refreshMinutes() != nil else { return }
        guard current != nil, !isExpired() else { return }
        let request = BGAppRefreshTaskRequest(identifier: Self.backgroundRefreshIdentifier)
        request.earliestBeginDate = Date(timeIntervalSinceNow: Double(refreshMinutes() ?? 15) * 60)
        try? BGTaskScheduler.shared.submit(request)
    }

    /// Enter the foreground: adopt an operator-started bubble and resume its
    /// loop. A foreground transition NEVER starts a new activity.
    func sceneDidBecomeActive() {
        guard setting().isActive else {
            endAndStopPolling()
            return
        }
        if current == nil { adoptExisting() }
        guard current != nil else { return }
        if isExpired() {
            endAndStopPolling()
            return
        }
        startPolling()
        scheduleBackgroundRefresh()
    }

    /// Leave the foreground: stop the tight loop, keep the bubble alive on
    /// OS-scheduled wakes.
    func sceneDidEnterBackground() {
        stopPolling()
        scheduleBackgroundRefresh()
    }

    // MARK: - Start / stop (operator controls)

    /// Start watching the cluster. Returns false when there is no usable
    /// connection or nothing to show, so the button does not claim a bubble
    /// that ActivityKit never received.
    func start() async -> Bool {
        guard setting().isActive, refreshMinutes() != nil else { return false }
        guard WidgetDataClient.makeIfConfigured() != nil else { return false }
        if current == nil {
            adoptExisting()
            if current == nil { startedAt = Date() }
        }
        await refreshOnce(startIfNeeded: true)
        guard current != nil else { return false }
        startPolling()
        scheduleBackgroundRefresh()
        return true
    }

    /// Stop watching and take the bubble down immediately.
    func stop() {
        endAndStopPolling()
    }

    // MARK: - Settings read

    /// This surface's settings, with the push-fed 15-minute default when the
    /// store has no entry for it yet.
    func setting() -> WidgetSurfaceSettings {
        AppGroupWidgetSettings.load()?.setting(for: .liveActivityClusterHealth)
            ?? WidgetSurfaceSettings(isActive: true,
                                     refreshMinutes: 15,
                                     showSections: Set(WidgetSurfaceKind.liveActivityClusterHealth.availableSections))
    }

    /// The poll cadence, or nil for the settings' "Off". A nil cadence stops
    /// both the foreground loop and the background submit; it never becomes a
    /// one-minute poll.
    private func refreshMinutes() -> Int? {
        let minutes = setting().refreshMinutes
        return minutes > 0 ? minutes : nil
    }

    // MARK: - Polling

    private func startPolling() {
        guard pollTask == nil else { return }
        guard current != nil, refreshMinutes() != nil else { return }
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                if self.isExpired() {
                    self.endAndStopPolling()
                    return
                }
                await self.refreshOnce()
                guard let next = self.refreshMinutes() else {
                    self.stopPolling()
                    return
                }
                try? await Task.sleep(nanoseconds: UInt64(next) * 60_000_000_000)
            }
        }
    }

    private func stopPolling() {
        pollTask?.cancel()
        pollTask = nil
    }

    // MARK: - Poll and push

    /// Read the sanctioned routes and push what they say. With
    /// `startIfNeeded` false this only updates an existing activity — a
    /// background wake must not resurrect a watch the operator stopped.
    func refreshOnce(startIfNeeded: Bool = false) async {
        guard setting().isActive else {
            endAndStopPolling()
            return
        }
        // Refresh "Off" stops the loop and leaves the existing bubble to age;
        // it must not become a one-minute poll, and it must not delete the
        // surface the operator only asked to stop refreshing.
        guard let cadence = refreshMinutes() else {
            stopPolling()
            return
        }
        guard let client = WidgetDataClient.makeIfConfigured() else {
            endAndStopPolling()
            return
        }
        if current == nil {
            adoptExisting()
            if current == nil && !startIfNeeded { return }
            if startedAt == nil { startedAt = Date() }
        }
        if isExpired() {
            endAndStopPolling()
            return
        }

        let showTally = setting().showSections.contains("nodes")

        let autodownOutcome = await WidgetData.read(using: client,
                                                    lastKnown: { self.lastAutodown }) {
            await $0.autodownStatus()
        }
        let (autodown, autodownLabel, autodownAt) = Self.resolved(autodownOutcome)
        var anyLive = autodownOutcome.isLive

        var monitor: MonitorResponse?
        var monitorLabel: String?
        var monitorAt: Date?
        if showTally {
            let monitorOutcome = await WidgetData.read(using: client,
                                                       lastKnown: { self.lastMonitor }) {
                await $0.clusterMonitor()
            }
            (monitor, monitorLabel, monitorAt) = Self.resolved(monitorOutcome)
            anyLive = anyLive || monitorOutcome.isLive
            if case .live(let value, let fetchedAt) = monitorOutcome {
                lastMonitor = (value, fetchedAt)
            }
        }

        let daemonOutcome = await WidgetData.read(using: client,
                                                  lastKnown: { self.lastDaemon }) {
            await $0.daemonHost()
        }
        let (daemon, _, daemonAt) = Self.resolved(daemonOutcome)
        anyLive = anyLive || daemonOutcome.isLive

        if case .live(let value, let fetchedAt) = autodownOutcome {
            lastAutodown = (value, fetchedAt)
        }
        if case .live(let value, let fetchedAt) = daemonOutcome {
            lastDaemon = (value, fetchedAt)
        }

        let signals = ClusterHealthProjection.signals(autodown: autodown,
                                                      autodownLabel: autodownLabel,
                                                      monitor: monitor,
                                                      monitorLabel: monitorLabel,
                                                      showNodeTally: showTally,
                                                      daemonRunning: daemon?.daemon_running)
        // The data's own clock: the OLDEST read this push draws, so a stale
        // snapshot can never be presented as newer than it is.
        let dataAt = ClusterHealthReading.dataDate([autodownAt, monitorAt, daemonAt]) ?? Date()
        let showingStale = autodownLabel != nil || monitorLabel != nil
        // New data arrived: let WidgetKit rebuild the Home Screen surfaces now
        // instead of waiting for its coalesced schedule (SPEC Refresh).
        if anyLive { WidgetCenter.shared.reloadAllTimelines() }
        await push(ClusterHealthActivityAttributes.ContentState(signals: signals,
                                                                updatedAt: dataAt),
                   cadenceMinutes: cadence,
                   showingStale: showingStale)
    }

    /// One read's outcome, reduced to what the projection consumes: the value it
    /// may draw, the label of whatever stopped it being live, and the date that
    /// value really belongs to.
    private static func resolved<V>(_ outcome: WidgetDataOutcome<V>)
        -> (value: V?, label: String?, at: Date?) {
        switch outcome {
        case .live(let value, let fetchedAt):
            return (value, nil, fetchedAt)
        case .stale(let value, let ageMinutes, let reason):
            return (value, reason.shortLabel,
                    ClusterHealthReading.snapshotDate(ageMinutes: ageMinutes))
        case .failed(let failure):
            return (nil, failure.shortLabel, nil)
        }
    }

    /// Push onto the activity, starting it the first time there is anything to
    /// show.
    private func push(_ content: ClusterHealthActivityAttributes.ContentState,
                      cadenceMinutes: Int,
                      showingStale: Bool) async {
        let stale = staleDate(for: content.updatedAt,
                              cadenceMinutes: cadenceMinutes,
                              showingStale: showingStale)
        if current == nil {
            guard !content.signals.isEmpty else { return }
            do {
                current = try Activity.request(
                    attributes: ClusterHealthActivityAttributes(startedAt: startedAt ?? Date()),
                    content: ActivityContent(state: content, staleDate: stale))
            } catch {
                current = nil
            }
            return
        }
        guard let current else { return }
        await current.update(ActivityContent(state: content, staleDate: stale))
    }

    /// The bubble ages from the DATA's own time, three cadences out — never
    /// from the failed refresh's clock. It also never outlives the eight-hour
    /// start-to-end budget.
    private func staleDate(for dataAt: Date,
                           cadenceMinutes: Int,
                           showingStale: Bool) -> Date {
        // A push carrying a failure label is already stale: date it from the
        // snapshot, not "now + three cycles", so a failed refresh cannot keep
        // extending a fresh-looking bubble.
        let dataStale = showingStale ? dataAt
            : dataAt.addingTimeInterval(Double(cadenceMinutes) * 60 * 3)
        if let startedAt {
            let runtimeEnd = startedAt.addingTimeInterval(ClusterHealthActivityAttributes.maxRuntime)
            return min(dataStale, runtimeEnd)
        }
        return dataStale
    }

    // MARK: - Adoption and expiry

    /// Adopt an activity the operator started before this process existed.
    /// The launch sweep has already removed orphans; this picks the newest
    /// surviving bubble and restores its start time.
    private func adoptExisting() {
        let activities = Activity<ClusterHealthActivityAttributes>.activities
        guard let activity = activities.max(by: { $0.content.state.updatedAt < $1.content.state.updatedAt }) else {
            return
        }
        current = activity
        startedAt = activity.attributes.startedAt ?? activity.content.state.updatedAt
    }

    private func isExpired() -> Bool {
        guard let startedAt else { return false }
        return Date().timeIntervalSince(startedAt) >= ClusterHealthActivityAttributes.maxRuntime
    }

    /// End the activity and stop polling (operator stopped it, surface turned
    /// off, or the eight-hour budget ran out).
    func endAndStopPolling() {
        stopPolling()
        startedAt = nil
        guard let current else { return }
        let activity = current
        self.current = nil
        Task {
            await activity.end(nil, dismissalPolicy: .immediate)
        }
    }
}
