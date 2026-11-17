import Foundation
import ActivityKit
import BackgroundTasks

// ---------------------------------------------------------------------------
// ClusterHealthActivityDriver — the APP side of the cluster-health Live Activity.
//
// Live Activities are PUSH-ONLY: the extension renders, and only the app process
// can call `update()`. `t_0120475a` shipped the attributes, the Island / Lock
// Screen views and the launch sweep; the bubble they render has been unfed ever
// since. This file is the feeder, and it is deliberately shaped like
// `MonitorActivityDriver` — the one Live Activity driver already proven on this
// app — so the two surfaces cannot drift in lifecycle, cadence or staleness.
//
// WHAT IT READS, and nothing else (docs/WIDGET_DATA_CONTRACT.md §3 dataset 5):
//
//   * `GET /v1/autodown/status`  → the serving state (`up` / `waking` / `down`).
//   * `GET /v1/cluster/monitor`  → the node tally, via `ClusterNodeTally` — the
//     SAME projection the cluster widget runs, so the widget and this bubble can
//     never disagree about how many nodes are answering.
//
// Both go through `WidgetData.read(using: WidgetDataClient.makeIfConfigured(),
// lastKnown:)`, which is the app's one stale-while-error rule: a live read is
// live, a failure that honest staleness covers yields the last-known payload
// WITH its reason, and anything else yields the failure. The driver keeps the
// last-known payloads IN MEMORY only — never the App Group. §7 keeps the
// activity's content out of shared storage (ActivityKit carries it), and a
// bubble that outlives the process is reconciled by the launch sweep rather than
// by a snapshot the next process might half-believe.
//
// WHAT IT DECIDES, which is nothing: every word, count, label and colour is
// produced by the sliced rules in `Sources/Shared/ClusterHealthActivity.swift`
// (`ClusterHealthProjection` + `ClusterHealthReading`) that
// `live_activity_rehydration_check.sh` executes. The driver's own decisions are
// scheduling ones — when to poll, whether to push, when to end.
//
// THE TOGGLE lives here, in the one place a settings read is allowed for this
// surface: `ClusterHealthActivitySweep` stays a pure rule with no store
// dependency so the gate can keep executing it (decision 5 of the card). The
// monitor driver's sweep reads `AppGroupWidgetSettings` and therefore cannot be
// gated; that half of its shape is deliberately NOT copied.
// ---------------------------------------------------------------------------

@MainActor
final class ClusterHealthActivityDriver {

    /// The singleton — the app owns exactly one cluster-health feeder.
    static let shared = ClusterHealthActivityDriver()

    /// The activity's background-refresh task identifier (Info.plist's
    /// `BGTaskSchedulerPermittedIdentifiers`, which `project.yml` generates).
    static let backgroundRefreshIdentifier = "com.hscc.ios.clusterhealth.backgroundRefresh"

    /// The in-flight activity, if any.
    private(set) var current: Activity<ClusterHealthActivityAttributes>?

    /// The foreground poll loop, while it is running.
    private var pollTask: Task<Void, Never>?

    /// Whether the BGTaskScheduler launch handler is installed. `submit()` for
    /// an identifier with no handler throws the fatal
    /// `NSInternalInconsistencyException` and takes the app with it, so every
    /// submit is gated on this — see `MonitorActivityDriver` for the full
    /// reasoning, which applies verbatim to this second identifier.
    private static var registered = false

    /// Last-known payloads, for stale-while-error. In-process only: the App
    /// Group never sees a wire payload (§7).
    private var lastAutodown: (value: AutodownStatusResponse, at: Date)?
    private var lastMonitor: (value: MonitorResponse, at: Date)?

    private init() {}

    // MARK: - App lifecycle hooks

    /// Register the background-refresh handler once at launch, from
    /// `didFinishLaunching`, BEFORE any scene can submit. Idempotent.
    ///
    /// The `register(forTaskWithIdentifier:)` CALL is what must be synchronous;
    /// the handler body runs later on a MainActor Task, which is fine.
    static func registerBackgroundTasks() {
        BGTaskScheduler.shared.register(
            forTaskWithIdentifier: backgroundRefreshIdentifier,
            using: nil
        ) { task in
            guard let task = task as? BGAppRefreshTask else { return }
            Task { @MainActor in
                // One best-effort push, then re-arm and report.
                await self.shared.refreshOnce()
                self.shared.scheduleBackgroundRefresh()
                task.setTaskCompleted(success: true)
            }
        }
        registered = true
    }

    /// Arm the next best-effort background wake. Only ever submits while the
    /// surface is active, and only after the handler is registered.
    func scheduleBackgroundRefresh() {
        guard Self.registered else { return }
        guard setting().isActive else { return }
        let request = BGAppRefreshTaskRequest(identifier: Self.backgroundRefreshIdentifier)
        request.earliestBeginDate = Date(timeIntervalSinceNow: Double(cadenceMinutes()) * 60)
        try? BGTaskScheduler.shared.submit(request)
    }

    /// Enter the foreground: resume the poll loop while the surface is on, and
    /// end anything lingering when the operator turned it off.
    func sceneDidBecomeActive() {
        guard setting().isActive else {
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

    // MARK: - Settings read

    /// This surface's settings, with the push-fed 15-minute default when the
    /// store has no entry for it yet (an install that predates this surface must
    /// keep showing the bubble exactly as it does today until the operator says
    /// otherwise).
    func setting() -> WidgetSurfaceSettings {
        AppGroupWidgetSettings.load()?.setting(for: .liveActivityClusterHealth)
            ?? WidgetSurfaceSettings(isActive: true,
                                     refreshMinutes: 15,
                                     showSections: Set(WidgetSurfaceKind.liveActivityClusterHealth.availableSections))
    }

    /// The poll cadence. `max(1, …)` for the same reason `MonitorActivityDriver`
    /// uses it: `0` is the settings UI's "Off", and an Off cadence must not
    /// become a tight loop — while a corrupt negative must not schedule a push
    /// 60 seconds in the past.
    private func cadenceMinutes() -> Int {
        max(1, setting().refreshMinutes)
    }

    // MARK: - Polling

    private func startPolling() {
        guard pollTask == nil else { return }
        let cadence = cadenceMinutes()
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

    // MARK: - Poll and push

    /// Read the two sanctioned routes and push what they say.
    ///
    /// Best-effort by construction: a route that fails contributes its failure
    /// label and nils only ITS OWN signal, so a monitor that degrades to
    /// `{"speak": …}` never blanks the serving state the other route just
    /// answered (§6 rule 1 — and it is why the counts go nil rather than 0).
    func refreshOnce() async {
        // Turned off since the loop was armed: drop the bubble rather than keep
        // pushing to a surface the operator switched off.
        guard setting().isActive else {
            endAndStopPolling()
            return
        }
        // No host/token in the shared store: there is no cluster to be healthy
        // or unhealthy about. `WidgetData` would answer `.failed(.notConfigured)`
        // and — correctly — refuse to call that an outage, but an unconfigured
        // app should not be showing a health bubble at all, so end it.
        guard let client = WidgetDataClient.makeIfConfigured() else {
            endAndStopPolling()
            return
        }

        let showTally = setting().showSections.contains("nodes")

        // Sequential, and only the reads this surface draws: §2 budgets ≤ 2
        // requests per surface per refresh, and the tally read is skipped
        // outright when the operator switched the tally off.
        let autodownOutcome = await WidgetData.read(using: client,
                                                    lastKnown: { self.lastAutodown }) {
            await $0.autodownStatus()
        }
        let (autodown, autodownLabel) = Self.resolved(autodownOutcome)

        var monitor: MonitorResponse?
        var monitorLabel: String?
        if showTally {
            let monitorOutcome = await WidgetData.read(using: client,
                                                       lastKnown: { self.lastMonitor }) {
                await $0.clusterMonitor()
            }
            (monitor, monitorLabel) = Self.resolved(monitorOutcome)
            if case .live(let value, let fetchedAt) = monitorOutcome {
                lastMonitor = (value, fetchedAt)
            }
        }
        if case .live(let value, let fetchedAt) = autodownOutcome {
            lastAutodown = (value, fetchedAt)
        }

        let signals = ClusterHealthProjection.signals(autodown: autodown,
                                                      autodownLabel: autodownLabel,
                                                      monitor: monitor,
                                                      monitorLabel: monitorLabel,
                                                      showNodeTally: showTally)
        await push(ClusterHealthActivityAttributes.ContentState(signals: signals,
                                                               updatedAt: Date()))
    }

    /// One read's outcome, reduced to what the projection consumes: the value it
    /// may draw (live or last-known) and the label of whatever stopped it being
    /// live. `WidgetDataOutcome.failure` is nil for `.stale` — the reason rides
    /// on the `.stale` case itself — so the three cases are unpacked here rather
    /// than assumed.
    private static func resolved<V>(_ outcome: WidgetDataOutcome<V>) -> (value: V?, label: String?) {
        switch outcome {
        case .live(let value, _):          return (value, nil)
        case .stale(let value, _, let reason): return (value, reason.shortLabel)
        case .failed(let failure):         return (nil, failure.shortLabel)
        }
    }

    /// Push onto the activity, starting it the first time there is anything to
    /// show.
    ///
    /// The guard is `signals.isEmpty` and nothing else, because that is exactly
    /// the condition the launch sweep ends a bubble for (`ClusterHealthOrphanRule`
    /// — a bubble with no state, no tally and no failure label has nothing any
    /// region can draw). Starting one anyway would mean the driver pushes a
    /// bubble and the next launch deletes it.
    private func push(_ content: ClusterHealthActivityAttributes.ContentState) async {
        if current == nil {
            guard !content.signals.isEmpty else { return }
            do {
                current = try Activity.request(
                    attributes: ClusterHealthActivityAttributes(),
                    content: ActivityContent(state: content, staleDate: staleDate()))
            } catch {
                current = nil
            }
            return
        }
        guard let current else { return }
        await current.update(ActivityContent(state: content, staleDate: staleDate()))
    }

    /// Three refresh cycles out, the `MonitorActivityDriver` precedent: a frozen
    /// bubble (force-quit, OS eviction) ages itself into "Stale" instead of
    /// going on claiming the fleet is serving, while one slow network is not
    /// immediately greyed.
    private func staleDate() -> Date {
        Date().addingTimeInterval(Double(cadenceMinutes()) * 60 * 3)
    }

    /// End the activity and stop polling (surface turned off, or unconfigured).
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
