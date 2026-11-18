import Foundation
import ActivityKit
import BackgroundTasks

// ---------------------------------------------------------------------------
// BoardProgressActivityDriver — the APP side of the per-board progress Live
// Activity.
//
// This surface is OPERATOR-STARTED, not ambient: the button lives on the
// project's Board screen, so there is exactly one board bubble at a time and
// its lifetime is the operator's Stop tap. It is still a Live Activity the
// operator can see, so since t_2b3084bb it has a `WidgetSurfaceKind` entry
// (`liveActivity.boardProgress`) and this driver consumes it: Active off
// suppresses a start and ends a bubble already up, 0 means no scheduled
// refresh, and the cadence replaces the old hard-coded 5 minutes. The board it
// watches still comes from the Board screen's start action, never the store.
//
// WHAT IT READS: the same board-status projection the Board Status widget
// draws. `GET /v1/mobile/summary?boards=<slug>` first, and only when that route
// is missing or cannot answer this board, the sanctioned
// `/v1/kanban/{stale,blocked}` interim through `BoardStatusReading.interim`.
// The last-known value is `BoardStatusSnapshotStore` — the SAME App Group blob
// the widget saves and reads, so the Lock Screen and the Home Screen cannot
// disagree about a board's numbers.
//
// WHAT IT DECIDES: scheduling only. Every count, word and tone is produced by
// `BoardProgressReading` in `Sources/Shared/BoardProgressActivity.swift`, and
// the wire → projection rules are `BoardStatusReading`'s. A refresh that did
// not answer pushes nil counts beside a failure label (drawn `—`), never zeros.
//
// Timing mirrors the other Live Activity drivers: a foreground poll loop on the
// surface's cadence, a best-effort `BGAppRefreshTask` while the activity is up
// (so the Lock Screen keeps moving when the app is backgrounded), and a
// `staleDate` three cycles out.
// ---------------------------------------------------------------------------

@MainActor
final class BoardProgressActivityDriver {

    /// The singleton — one bubble, one feeder.
    static let shared = BoardProgressActivityDriver()

    /// The activity's background-refresh identifier (`BGTaskSchedulerPermittedIdentifiers`
    /// in project.yml — a submit for an identifier that is not listed there fails).
    static let backgroundRefreshIdentifier = "com.hscc.ios.boardprogress.backgroundRefresh"

    /// The cadence an install that never opened Settings gets: the board-status
    /// widget's 5 minutes, because this surface reads the same route and shows
    /// the same numbers. A stored `liveActivity.boardProgress` entry overrides
    /// it through `setting()`.
    static let defaultRefreshMinutes = 5

    /// This surface's settings, with the Live Activity defaults when no entry
    /// has ever been written. The same bounded read path every other driver
    /// uses, so the row in Settings and this poll loop cannot disagree.
    private func setting() -> WidgetSurfaceSettings {
        AppGroupWidgetSettings.load()?.setting(for: .liveActivityBoardProgress)
            ?? WidgetSurfaceSettings(isActive: true,
                                     refreshMinutes: Self.defaultRefreshMinutes,
                                     showSections: Set(WidgetSurfaceKind.liveActivityBoardProgress.availableSections))
    }

    /// The in-flight activity, if this process started or adopted one.
    private(set) var current: Activity<BoardProgressActivityAttributes>?

    /// The board the current activity watches; nil when none is up.
    private var board: String?

    /// The foreground poll loop, while it is running.
    private var pollTask: Task<Void, Never>?

    /// Whether the BGTaskScheduler launch handler is installed. Submitting
    /// before registering throws the fatal `NSInternalInconsistencyException`,
    /// so every submit gates on this.
    private static var registered = false

    private init() {}

    // MARK: - App lifecycle hooks

    /// Register the background-refresh handler once at launch, synchronously
    /// from `didFinishLaunching`, before any scene can submit. Idempotent.
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

    /// Ask the OS for one best-effort wake while an activity is up. A paused
    /// surface, or Off (0) cadence, schedules nothing.
    func scheduleBackgroundRefresh() {
        let surface = setting()
        guard Self.registered, current != nil,
              surface.isActive, surface.refreshMinutes > 0 else { return }
        let request = BGAppRefreshTaskRequest(identifier: Self.backgroundRefreshIdentifier)
        request.earliestBeginDate = Date(timeIntervalSinceNow: Double(surface.refreshMinutes) * 60)
        try? BGTaskScheduler.shared.submit(request)
    }

    /// Foreground: adopt a bubble left by a previous process, then poll it.
    /// A paused surface ends the bubble instead of resuming it.
    func sceneDidBecomeActive() {
        adoptExistingIfNeeded()
        guard setting().isActive else {
            stop()
            return
        }
        guard current != nil else { return }
        startPolling()
        scheduleBackgroundRefresh()
    }

    /// Background: stop the tight loop and let the OS own the next push.
    func sceneDidEnterBackground() {
        stopPolling()
        scheduleBackgroundRefresh()
    }

    /// A Live Activity outlives the process, so an activity from before a
    /// relaunch can already be on screen while `current` is nil. Adopt it
    /// rather than starting a second bubble, and resume its board.
    private func adoptExistingIfNeeded() {
        guard current == nil,
              let found = Activity<BoardProgressActivityAttributes>.activities.first else { return }
        current = found
        board = found.attributes.board
    }

    // MARK: - Start / stop from the project's Board screen

    /// Start watching one board, replacing any other board's bubble.
    ///
    /// Returns whether a bubble is up afterwards: ActivityKit can refuse (the
    /// operator turned Live Activities off, or the device is at its limit), and
    /// the button must not then offer to stop something that does not exist.
    func start(board: String) async -> Bool {
        guard setting().isActive else {
            stop()
            return false
        }
        let others = Activity<BoardProgressActivityAttributes>.activities
        for activity in others where activity.attributes.board != board {
            await activity.end(nil, dismissalPolicy: .immediate)
        }
        self.board = board
        current = others.first { $0.attributes.board == board }
        await refreshOnce()
        startPolling()
        scheduleBackgroundRefresh()
        return current != nil
    }

    /// Stop watching: end the bubble and stop polling.
    func stop() {
        stopPolling()
        board = nil
        let held = current
        current = nil
        if let held {
            Task { await held.end(nil, dismissalPolicy: .immediate) }
        }
        for activity in Activity<BoardProgressActivityAttributes>.activities {
            Task { @MainActor in
                await activity.end(nil, dismissalPolicy: .immediate)
            }
        }
    }

    /// Whether the bubble currently watches this board. The Board screen uses
    /// it to label its start/stop button honestly.
    func isRunning(board: String) -> Bool {
        if let current { return current.attributes.board == board }
        return Activity<BoardProgressActivityAttributes>.activities
            .contains { $0.attributes.board == board }
    }

    // MARK: - Polling

    private func startPolling() {
        guard pollTask == nil, current != nil else { return }
        let minutes = setting().refreshMinutes
        // Off (0) is "shown but no scheduled refresh": keep the bubble that is
        // already up, stop the loop. The Board screen's manual start still
        // performs its one refresh before this point.
        guard minutes > 0 else { return }
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refreshOnce()
                try? await Task.sleep(nanoseconds: UInt64(minutes) * 60_000_000_000)
            }
        }
    }

    private func stopPolling() {
        pollTask?.cancel()
        pollTask = nil
    }

    // MARK: - One refresh

    /// Read the board's counts through the sanctioned route pair, project them,
    /// and push. Best-effort: every failure path ends in a state the surface can
    /// render (last-known counts with a label, or empty counts with a label),
    /// and none can fabricate a count.
    func refreshOnce() async {
        adoptExistingIfNeeded()
        guard setting().isActive, let board else { return }

        guard let client = WidgetDataClient.makeIfConfigured() else {
            await push(Self.emptyState(label: WidgetDataFailure.notConfigured.shortLabel,
                                       detail: WidgetDataFailure.notConfigured.operatorLine))
            return
        }
        let outcome = await WidgetData.read(
            using: client,
            lastKnown: { BoardStatusSnapshotStore.load(boardSlug: board) }
        ) { c in
            await Self.readCounts(client: c, boardSlug: board)
        }

        let projection: BoardStatusProjection?
        let failureLabel: String?
        let failureDetail: String?
        switch outcome {
        case .live(let value, let fetchedAt):
            // Save what the widget will also read, then push it label-free.
            BoardStatusSnapshotStore.save(value, at: fetchedAt)
            projection = value
            failureLabel = nil
            failureDetail = nil
        case .stale(let value, _, let reason):
            // Real numbers, real age, and the reason this refresh did not
            // answer all travel together, so a stale bubble is never quiet.
            projection = value
            failureLabel = reason.shortLabel
            failureDetail = reason.operatorLine
        case .failed(let failure):
            // Nothing usable: the counts go nil (drawn `—`) and the label is
            // the whole headline.
            projection = nil
            failureLabel = failure.shortLabel
            failureDetail = failure.operatorLine
        }

        await push(Self.contentState(from: projection, failureLabel: failureLabel,
                                     failureDetail: failureDetail))
    }

    /// The five counts one push carries (the CLI board table's columns). A
    /// projection only exists for a read that answered; every absent status
    /// stays nil all the way to the view.
    static func contentState(from projection: BoardStatusProjection?,
                             failureLabel: String?,
                             failureDetail: String? = nil) -> BoardProgressActivityAttributes.ContentState {
        BoardProgressActivityAttributes.ContentState(
            done: projection?.done,
            running: projection?.running,
            blocked: projection?.blocked,
            ready: projection?.ready,
            todo: projection?.todo,
            failureLabel: failureLabel,
            failureDetail: failureDetail,
            updatedAt: Date())
    }

    private static func emptyState(label: String, detail: String? = nil) -> BoardProgressActivityAttributes.ContentState {
        contentState(from: nil, failureLabel: label, failureDetail: detail)
    }

    /// The board-status route policy, matching `BoardStatusTimelineProvider`:
    /// §6's compact summary first, §5.2's interim second. The summary names the
    /// board with `?boards=`; the interim routes ignore that parameter
    /// (measured, §4.3), so `BoardStatusReading.interim` scopes client-side.
    private static func readCounts(client: WidgetDataClient,
                                   boardSlug: String?) async -> Result<BoardStatusProjection, WidgetDataFailure> {
        switch await client.mobileSummary(boards: boardSlug.map { [$0] } ?? []) {
        case .success(let response):
            if let projection = BoardStatusProjection(summary: response, boardSlug: boardSlug) {
                return .success(projection)
            }
            // Degraded, or this board carried its own `error`: neither is a
            // transport failure, so the interim path is tried before giving up.
        case .failure:
            break  // missing route, unreachable, bad token: the interim decides what to say
        }

        async let staleRead = client.kanbanStale(olderThanDays: 0)
        async let blockedRead = client.kanbanBlocked()
        let (stale, blocked) = await (staleRead, blockedRead)
        return BoardStatusReading.interim(stale: stale, blocked: blocked, boardSlug: boardSlug)
    }

    // MARK: - Push

    /// Start the bubble if it is not up, otherwise update it.
    private func push(_ content: BoardProgressActivityAttributes.ContentState) async {
        guard let board else { return }
        if current == nil {
            if let found = Activity<BoardProgressActivityAttributes>.activities
                .first(where: { $0.attributes.board == board }) {
                current = found
                await found.update(ActivityContent(state: content, staleDate: staleDate()))
                return
            }
            do {
                current = try Activity.request(
                    attributes: BoardProgressActivityAttributes(board: board),
                    content: ActivityContent(state: content, staleDate: staleDate()))
            } catch {
                // ActivityKit can refuse. Nothing to render is not a failure to
                // report: the Board screen still shows the same counts.
                current = nil
            }
            return
        }
        guard let current else { return }
        await current.update(ActivityContent(state: content, staleDate: staleDate()))
    }

    /// Three refresh cycles out: a bubble whose feeder died ages into "Stale"
    /// instead of claiming the counts are current, while one slow network tick
    /// does not grey it out. Same formula as the sibling drivers.
    private func staleDate() -> Date {
        // Off (0) still needs a stale ceiling: three default cycles out rather
        // than "never", so a bubble whose feeder died cannot claim it is live.
        let minutes = max(1, setting().refreshMinutes)
        return Date().addingTimeInterval(Double(minutes) * 60 * 3)
    }
}
