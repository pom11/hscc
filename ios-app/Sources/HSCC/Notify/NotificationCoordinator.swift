import Foundation
import UserNotifications

// ===========================================================================
// NotificationCoordinator — foreground delivery of needs-operator alerts.
//
// Phase 1+2 of docs/notify-operator-plan.md. This @MainActor singleton is the
// FOREGROUND path: it polls the condition endpoints while the app is running
// and surfaces NEW needs-operator conditions as local UNUserNotifications.
//
// Pipeline (all the clever parts live in the pure `NeedsOperatorNotifier`):
//   (a) build an `ObservedState` by calling the client's endpoints,
//   (b) feed it to `NeedsOperatorNotifier.compute(prior:now:)`,
//   (c) fire the resulting alerts via UNUserNotificationCenter,
//   (d) persist the new lastSeen + announced via `NeedsOperatorNotifier.nextState`.
//
// The foreground poll loop mirrors the existing ApprovalPoller / StreamReplyWatcher
// pattern (`setClient` + a repeating Timer). A future Phase 3 BGAppRefreshTask
// handler can call the SAME `refresh()` — the seam is the engine, not the
// transport, so nothing here needs to change to add background delivery.
//
// Honest limits called out by the plan:
//   * Authorization denied → refresh() no-ops; alerting is silently off, never
//     a crash. Settings reads authorization state so it tells the truth.
//   * No cluster configured → `setClient(nil)` stops the loop; nothing polls.
//   * A transient poll hiccup is treated as inconclusive (nil), NOT a confident
//     "down" — the engine refuses to spam on a flapping link.
// ===========================================================================

/// Per-event-class notification toggles, backed by the shared App-Group suite
/// so Settings and the coordinator agree, and a future widget/background path
/// reads the same store.
enum NotifyPreferences {
    static let kindKey = { (kind: OperatorAlert.Kind) -> String in
        "hscc.notify.enabled." + kind.rawValue
    }

    private static var suite: UserDefaults {
        UserDefaults(suiteName: AppGroup.suiteName) ?? .standard
    }

    /// Each class defaults to ON. The operator can turn a class off in Settings
    /// to control exactly how much interruption.
    static func isEnabled(for kind: OperatorAlert.Kind) -> Bool {
        suite.object(forKey: kindKey(kind)) == nil
            ? true
            : suite.bool(forKey: kindKey(kind))
    }

    static func setEnabled(_ enabled: Bool, for kind: OperatorAlert.Kind) {
        suite.set(enabled, forKey: kindKey(kind))
    }
}

@MainActor
final class NotificationCoordinator {

    static let shared = NotificationCoordinator()

    /// Foreground poll cadence — matches the cluster tab's other foreground
    /// pollers (ApprovalPoller: 60s, StreamReplyWatcher: 30s). Chosen so a
    /// genuinely-new condition is surfaced within about a minute while the app
    /// stays open.
    private static let pollInterval: TimeInterval = 60

    private var client: HSCCClient?
    private var timer: Timer?
    /// Authorization state, refreshed each refresh cycle so Settings stays
    /// honest about whether notifications can even fire.
    private var authorizationAuthorized = false

    private init() {}

    // MARK: - One-time authorization (called from the app delegate)

    /// Request notification authorization if not already determined. This is
    /// the app's ONLY call into the permission prompt — invoked once at launch
    /// by the `NotificationsAppDelegate`.
    func requestAuthorization() async {
        let center = UNUserNotificationCenter.current()
        let settings = await center.notificationSettings()
        guard settings.authorizationStatus == .notDetermined else {
            authorizationAuthorized = (settings.authorizationStatus == .authorized
                                       || settings.authorizationStatus == .provisional)
            return
        }
        let granted = try? await center.requestAuthorization(options: [.alert, .sound, .badge])
        authorizationAuthorized = (granted ?? false)
    }

    // MARK: - Foreground poll loop

    /// Attach the current client (or nil when unconfigured). Restarts the poll.
    /// Called by ContentView alongside the other pollers whenever the active
    /// cluster changes.
    func setClient(_ client: HSCCClient?) {
        self.client = client
        timer?.invalidate()
        timer = nil
        guard client != nil else { return }
        Task { await refresh() }
        timer = Timer.scheduledTimer(withTimeInterval: Self.pollInterval,
                                     repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in await self?.refresh() }
        }
    }

    /// One full poll cycle: build the observation, run the engine, fire any new
    /// alerts, persist the new state. Safe to call from foreground or a future
    /// background refresh handler.
    func refresh() async {
        guard let client else { return }
        // Authorization denied → alerting silently off (no poll, no crash).
        let settings = await UNUserNotificationCenter.current().notificationSettings()
        authorizationAuthorized = (settings.authorizationStatus == .authorized)
                                  || (settings.authorizationStatus == .provisional)
        guard authorizationAuthorized else { return }

        let prior = LastSeenStateStore.load()
        let now = await buildObservedState(client: client)
        let alerts = NeedsOperatorNotifier.compute(prior: prior, now: now)
        if !alerts.isEmpty {
            await fire(alerts)
        }
        LastSeenStateStore.save(NeedsOperatorNotifier.nextState(prior: prior, now: now))
    }

    // MARK: - Observation assembly

    /// Poll the endpoints into one `ObservedState`.
    ///
    /// Reachability is determined FIRST by a single probe, because every other
    /// condition endpoint's output is meaningless (or its failure is noise, not
    /// signal) when we cannot reach the API. A transport failure on the probe
    /// is the confident "down"; an HTTP response (even an API error like a 401)
    /// proves reachable; anything else is inconclusive (nil).
    private func buildObservedState(client: HSCCClient) async -> ObservedState {
        var state = ObservedState()

        do {
            let daemon = try await client.daemonStatus()
            state.apiReachable = true
            state.daemonRunning = daemon.daemon_running
        } catch {
            if let e = error as? HSCCError {
                switch e {
                case .transport:
                    // Definite transport failure = confident "down".
                    state.apiReachable = false
                case .api:
                    // Reached the API with an HTTP response (e.g. 401) — reachable.
                    state.apiReachable = true
                default:
                    // Decoding weirdness — inconclusive, never claim down.
                    state.apiReachable = nil
                }
            } else {
                state.apiReachable = nil
            }
        }

        // Only poll the condition endpoints when the API is provably reachable;
        // otherwise their transport failures would be misread as conditions.
        guard state.apiReachable == true else { return state }

        // Each condition read is best-effort: a single transient failure of one
        // endpoint must not blank a condition into "nothing" (that would clear
        // announced state and cause a spurious re-announce later). On failure we
        // keep the endpoint's unknown (empty) — the differential engine will
        // simply not see a change for it this cycle.
        if let review = try? await client.reviewQueue() {
            // Use a STABLE key (card_id → title → project), never the volatile
            // `ReviewQueueRow.id` which falls back to a fresh UUID per poll — a
            // card with no identifiers would otherwise get a new id every cycle
            // and be re-announced forever. Rows with no stable identifier drop
            // out of the dedup (they can't be tracked reliably) rather than spam.
            state.reviewQueue = Set(
                review.queue.compactMap { $0.card_id ?? $0.title ?? $0.project }
            )
        }
        if let blocked = try? await client.kanbanBlocked() {
            state.blocked = Set((blocked.tasks ?? []).map { $0.id })
        }
        if let esc = try? await client.escalations() {
            state.escalationsCount = esc.count ?? 0
        }
        return state
    }

    // MARK: - Delivery

    /// Fire delivered alerts through UNUserNotificationCenter, honoring the
    /// per-event-class toggles (a class the operator turned off is skipped).
    private func fire(_ alerts: [OperatorAlert]) async {
        let center = UNUserNotificationCenter.current()
        for alert in alerts where NotifyPreferences.isEnabled(for: alert.kind) {
            let content = UNMutableNotificationContent()
            content.title = alert.title
            content.body = alert.body
            content.sound = alert.sound ? .default : nil
            content.threadIdentifier = alert.threadIdentifier
            let request = UNNotificationRequest(
                identifier: "hscc.notify.\(alert.kind.rawValue).\(UUID().uuidString)",
                content: content,
                trigger: nil   // deliver immediately
            )
            try? await center.add(request)
        }
    }

    // MARK: - Test button + authorization reflection

    /// Fire a sample alert for the Settings "Test notification" button.
    /// Gated on authorization being granted; no-op (silently) otherwise.
    func testNotification() async {
        let settings = await UNUserNotificationCenter.current().notificationSettings()
        guard settings.authorizationStatus == .authorized
                || settings.authorizationStatus == .provisional else { return }
        await fire([OperatorAlert(
            kind: .needsReview,
            title: "HSCC test notification",
            body: "If you can see this, alerts are working.",
            sound: true
        )])
    }

    /// Reflect current authorization so Settings can tell the truth about
    /// whether notifications can fire. Refreshed by `requestAuthorization` and
    /// every `refresh()`.
    var isAuthorized: Bool {
        authorizationAuthorized
    }

    /// Whether any notifications can currently be delivered — used by Settings
    /// to show an honest banner when the user enabled toggles but denied (or
    /// hasn't granted) authorization.
    var canDeliver: Bool {
        authorizationAuthorized
    }
}
