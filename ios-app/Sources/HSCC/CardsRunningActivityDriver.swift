import Foundation
import ActivityKit
import BackgroundTasks

// ---------------------------------------------------------------------------
// CardsRunningActivityDriver — the APP side of the running-cards Live Activity.
//
// A Live Activity is PUSH-ONLY: the extension renders, it cannot fetch. The app
// is the only process that can call `update()`, and until this file nothing did
// — t_f9cc34e0 shipped the rendering, the launch sweep and the gate, and left
// the surface unfed on purpose, because honouring the shared Active toggle and
// cadence needs a `WidgetSurfaceKind` case (t_71deac83).
//
// What one refresh does:
//
//   * read the pair the widget reads — `GET /v1/kanban/running` +
//     `GET /v1/kanban/blocked`, through `WidgetData.read` so the stale-while-error
//     rule is the shared one and the last-known value is the SAME App Group
//     snapshot the Home Screen widget saves (one blob, one decoder);
//   * project it with `CardsRunningProjection`, the widget's own projection, so
//     the Island and the Home Screen cannot disagree about what a row means or
//     about whether a number was ever sent;
//   * decide with `CardsRunningFeedRule.decide` (a sliced, gate-executed rule in
//     `Sources/Shared/CardsRunningActivity.swift`) whether to start/update, END,
//     or leave the activity alone.
//
// The two rules that make this surface honest, both in that sliced block and
// both asserted by `live_activity_rehydration_check.sh`:
//
//   * **a blind read never produces `runningCount = 0`.** The count a push
//     carries is the projection's, and a projection only exists for a read that
//     answered; a refresh that did not answer pushes `nil` (which the views draw
//     `—`) beside a `failureLabel` that says why.
//   * **nothing running means the activity ENDS**, not an empty bubble: the
//     driver reaches the same conclusion at the same moment the launch sweep
//     would (`CardsRunningOrphanRule.isOrphan`), so the app never pushes a
//     bubble the next launch immediately kills.
//
// Timing, mirroring `MonitorActivityDriver` (the sibling driver) exactly:
//
//   * foreground → a poll loop on the surface's cadence;
//   * background → the loop stops and a `BGAppRefreshTask` is submitted, so the
//     OS can wake the app for one best-effort push (its schedule, not ours);
//   * `refreshMinutes == 0` is the config screen's "Off": no loop, no submit.
//     The bubble stays where it is and ages through its own `staleDate`; the
//     operator said "do not refresh this", and a driver that pushed anyway
//     would be contradicting the one control that surface has;
//   * `isActive == false` → end the activity and stop polling, decided BEFORE
//     any client is built, so a paused surface sends zero requests.
//
// The scope question, answered: `AppGroupWidgetSettings.scope(for:)` is
// deliberately NOT consulted. `/v1/kanban/running` and `/v1/kanban/blocked`
// were measured to ignore `?board=` (WIDGET_DATA_CONTRACT §4.3), so this
// surface is fleet-wide whatever the store holds — which is also why
// `WidgetSurfaceKind.supportsScope` is false for it and the config screen offers
// it no scope picker.
// ---------------------------------------------------------------------------

@MainActor
final class CardsRunningActivityDriver {

    /// The singleton — the app owns exactly one running-cards feeder.
    static let shared = CardsRunningActivityDriver()

    /// The activity's background refresh identifier (`BGTaskSchedulerPermittedIdentifiers`
    /// in project.yml — a submit for an identifier that is not listed there fails).
    static let backgroundRefreshIdentifier = "com.hscc.ios.cards.backgroundRefresh"

    /// The in-flight activity, if this process started or adopted one.
    private(set) var current: Activity<CardsRunningActivityAttributes>?

    /// The foreground poll loop, while it is running.
    private var pollTask: Task<Void, Never>?

    /// Whether `BGTaskScheduler` has the launch handler installed.
    /// `submit()` before `register(forTaskWithIdentifier:)` throws the fatal
    /// `NSInternalInconsistencyException`, so every submit gates on this flag —
    /// see `MonitorActivityDriver.registerBackgroundTasks` for the full account
    /// of that crash. Same rule, same reason, second identifier.
    private static var registered = false

    private init() {}

    // MARK: - App lifecycle hooks

    /// Register this activity's background-refresh handler once at launch,
    /// synchronously from `didFinishLaunching`, before any scene can submit.
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

    /// Ask the OS for one best-effort wake at the surface's cadence. Only when
    /// the surface is active and the cadence is not "Off".
    func scheduleBackgroundRefresh() {
        guard Self.registered else { return }
        let surface = setting()
        guard surface.isActive, surface.refreshMinutes > 0 else { return }
        let request = BGAppRefreshTaskRequest(identifier: Self.backgroundRefreshIdentifier)
        request.earliestBeginDate = Date(timeIntervalSinceNow: Double(surface.refreshMinutes) * 60)
        try? BGTaskScheduler.shared.submit(request)
    }

    // MARK: - Settings read

    /// This surface's settings, with the Live Activity defaults when the store
    /// has no entry for it yet (a surface that predates the blob keeps working).
    func setting() -> WidgetSurfaceSettings {
        AppGroupWidgetSettings.load()?.setting(for: .liveActivityCards)
            ?? WidgetSurfaceSettings(isActive: true,
                                     refreshMinutes: 15,
                                     showSections: Set(WidgetSurfaceKind.liveActivityCards.availableSections))
    }

    // MARK: - Start / stop driven by scene phase + settings

    /// Foreground: honour the toggle first, then poll.
    func sceneDidBecomeActive() {
        guard setting().isActive else {
            endAndStopPolling()
            return
        }
        startPolling()
        scheduleBackgroundRefresh()
    }

    /// Background: stop the tight loop (battery) and let the OS own the next push.
    func sceneDidEnterBackground() {
        stopPolling()
        scheduleBackgroundRefresh()
    }

    // MARK: - Polling

    private func startPolling() {
        guard pollTask == nil else { return }
        let cadence = setting().refreshMinutes
        guard cadence > 0 else { return }   // "Off" is Off: no loop, no requests
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

    // MARK: - One refresh

    /// Read the pair, project it, and do whatever the rule says with the result.
    ///
    /// Best-effort by construction: every failure path here ends in a state the
    /// surface can render (a stale snapshot with its label, or a blind push), and
    /// none of them can fabricate a count.
    func refreshOnce() async {
        // Decided before any client exists: a paused surface sends zero requests.
        guard setting().isActive else {
            endAndStopPolling()
            return
        }
        let client = WidgetDataClient.makeIfConfigured()
        // No host, no token: nothing can ever fill this bubble, and an
        // unconfigured app invites setup — it does not render a cluster claim.
        guard client != nil else {
            endAndStopPolling()
            return
        }
        let outcome = await WidgetData.read(using: client,
                                           lastKnown: { CardsRunningSnapshotStore.load() },
                                           { c in await CardsRunningFeed.read(client: c) })

        let projection: CardsRunningProjection?
        let failureLabel: String?
        switch outcome {
        case .live(let value, let fetchedAt):
            // Save what the widget will also read, then push it label-free.
            CardsRunningSnapshotStore.save(value, at: fetchedAt)
            projection = value
            failureLabel = CardsRunningFeedRule.label(reason: nil, live: true)
        case .stale(let value, _, let reason):
            // Real rows, real age, and the reason this refresh did not answer —
            // all three travel together, so a stale bubble is never quiet.
            projection = value
            failureLabel = CardsRunningFeedRule.label(reason: reason.shortLabel, live: false)
        case .failed(let failure):
            // Nothing usable. The rows are absent, so the count is `nil` and
            // never `0`, and the label is the whole headline.
            projection = nil
            failureLabel = CardsRunningFeedRule.label(reason: failure.shortLabel, live: false)
        }

        let state = Self.contentState(from: projection, failureLabel: failureLabel)
        switch CardsRunningFeedRule.decide(activityIsUp: current != nil,
                                           rowCount: state.rows.count,
                                           failureLabel: failureLabel) {
        case .push:
            await push(state)
        case .end:
            endEveryActivity()
        case .idle:
            // No activity up and nothing worth starting — the honest do-nothing
            // case (a blind first refresh must not raise a hollow bubble).
            break
        }
    }

    // MARK: - Projection onto the activity's own content type

    /// The activity's rows, clipped to the surface's own limit.
    ///
    /// `CardsRunningRow` → `RunningCardMetric` is field for field, so the Home
    /// Screen and the Island draw the same five scalars for the same card; the
    /// types stay separate because the App Group must hold no payload type (§7)
    /// while this one is ActivityKit's and never persisted.
    static func metrics(from projection: CardsRunningProjection?) -> [RunningCardMetric] {
        guard let projection else { return [] }
        return projection.rows.prefix(CardsRunningActivityAttributes.rowLimit).map { row in
            RunningCardMetric(board: row.board,
                              cardID: row.cardID,
                              title: row.title,
                              worker: row.worker,
                              status: row.status,
                              ageMinutes: row.ageMinutes)
        }
    }

    /// The ContentState one push carries. `runningCount` / `blockedCount` come
    /// straight from the projection, so "not told" stays `nil` (drawn `—`) all
    /// the way to the view instead of becoming a zero on the way past.
    static func contentState(from projection: CardsRunningProjection?,
                             failureLabel: String?) -> CardsRunningActivityAttributes.ContentState {
        CardsRunningActivityAttributes.ContentState(
            rows: metrics(from: projection),
            runningCount: projection?.runningCount,
            blockedCount: projection?.blockedCount,
            rowsAreComplete: projection?.rowsAreComplete ?? false,
            failureLabel: failureLabel,
            updatedAt: Date())
    }

    // MARK: - Push

    /// Start the activity on the first push worth showing, otherwise update it.
    private func push(_ state: CardsRunningActivityAttributes.ContentState) async {
        if current == nil {
            // A Live Activity outlives the process, so an activity from before a
            // relaunch can already be on screen while `current` is nil. Adopt it
            // rather than requesting a second bubble for the same fleet.
            if let found = Activity<CardsRunningActivityAttributes>.activities.first {
                current = found
                await found.update(ActivityContent(state: state, staleDate: staleDate()))
                return
            }
            do {
                current = try Activity.request(
                    attributes: CardsRunningActivityAttributes(),
                    content: ActivityContent(state: state, staleDate: staleDate()))
            } catch {
                // ActivityKit can refuse (the user turned Live Activities off, or
                // the device is at its limit). Nothing to render is not a failure
                // to report: the widget still shows the same two routes.
                current = nil
            }
            return
        }
        guard let current else { return }
        await current.update(ActivityContent(state: state, staleDate: staleDate()))
    }

    /// ~3 refresh cycles out, so a bubble whose feeder died ages into "Stale"
    /// visually instead of lying as current, while one hung network tick does not
    /// grey it out. Same formula as the monitor's driver.
    private func staleDate() -> Date {
        Date().addingTimeInterval(Double(max(1, setting().refreshMinutes)) * 60 * 3)
    }

    // MARK: - End

    /// End every running-cards activity: the one this process holds, and any the
    /// launch found that this process never adopted.
    ///
    /// A driver that ended only its own handle would leave the bubble a previous
    /// process left behind on the Lock Screen, and the operator cannot tell the
    /// two apart from out there — the switch would look broken. `adopt` never
    /// happened in that case, so this is the other half of the same fact.
    private func endEveryActivity() {
        let held = current
        current = nil
        if let held {
            Task {
                await held.end(nil, dismissalPolicy: .immediate)
            }
        }
        for found in Activity<CardsRunningActivityAttributes>.activities where found.id != held?.id {
            Task { @MainActor in
                await found.end(nil, dismissalPolicy: .immediate)
            }
        }
    }

    /// Surface turned off / not configured / nothing running: stop polling and
    /// take every bubble down rather than leaving an empty one up.
    func endAndStopPolling() {
        stopPolling()
        endEveryActivity()
    }
}
