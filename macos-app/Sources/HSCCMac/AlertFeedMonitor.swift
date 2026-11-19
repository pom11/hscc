import Foundation
import UserNotifications

// ---------------------------------------------------------------------------
// AlertFeedMonitor.swift — the macOS alert path (card N4).
//
// The phone gets alerts through APNs; the Mac polls. That choice is deliberate:
// a Developer ID + notarized build would need the `aps-environment` entitlement
// and its provisioning story to receive remote pushes, while `GET /v1/alerts`
// is already authenticated, already durable and already cheap (a few hundred
// bytes). Every 5 s the monitor advances the same `since` cursor the route's
// contract defines and posts one LOCAL notification per new alert — no push
// entitlement, no signing change, no device token on the Mac.
//
// Scope is "while running": the app is LSUIElement, so the loop lives for the
// process lifetime. A user-initiated activity keeps App Nap from coalescing the
// 5 s cadence; it explicitly allows the system to sleep. A first launch (or a
// cleared feed) is a baseline and posts nothing — reopening the app must not
// replay history as banners.
//
// State is per cluster and lives in the shared App Group: the cursor, the
// viewed id and the unseen count survive a Sparkle relaunch, and switching
// clusters switches cursors instead of comparing ids from two feeds.
// ---------------------------------------------------------------------------

@MainActor
final class AlertFeedMonitor: NSObject, ObservableObject {
    /// The design's Mac cadence: cheap enough to poll, fast enough that a new
    /// alert is on screen within seconds.
    static let pollInterval: TimeInterval = 5

    @Published private(set) var state = AlertFeedState()
    /// The dropdown tells the truth when macOS permission was denied or revoked
    /// in System Settings; rows and the badge still work without banners.
    @Published private(set) var notificationsDenied = false

    private let defaults: UserDefaults
    private var loadedClusterKey: String?
    private var task: Task<Void, Never>?
    private var activity: NSObjectProtocol?
    private var authorizationRequested = false
    private var lastAuthorizationCheck: Date?

    var rows: [AlertFeedRow] { state.rows }
    var unseenCount: Int { state.unseen }

    override init() {
        defaults = UserDefaults(suiteName: AppGroup.suiteName) ?? .standard
        super.init()
        // The same delegate presents a banner even when the menu bar app is the
        // active app; without it macOS suppresses foreground notifications.
        UNUserNotificationCenter.current().delegate = self
        activity = ProcessInfo.processInfo.beginActivity(
            options: .userInitiatedAllowingIdleSystemSleep,
            reason: "Poll the HSCC alert feed every 5 seconds")
        task = Task { [weak self] in
            await self?.runLoop()
        }
    }

    deinit {
        task?.cancel()
        if let activity {
            ProcessInfo.processInfo.endActivity(activity)
        }
    }

    // MARK: - Menu surface

    /// Called when the dropdown closes. The alerts were already delivered as
    /// banners; this clears the count beside "Alerts" without touching the
    /// server (ack is the operator's decision in the main orchestrator's pane,
    /// not a menu bar side effect).
    func markViewed() {
        guard state.unseen > 0 || state.viewedID != state.knownID else { return }
        state.viewedID = state.knownID
        state.unseen = 0
        persist()
    }

    /// "Refresh now" in the dropdown refreshes the feed with the rest.
    func refreshNow() async {
        await pollOnce()
    }

    // MARK: - Poll loop

    private func runLoop() async {
        while !Task.isCancelled {
            await pollOnce()
            try? await Task.sleep(nanoseconds: UInt64(Self.pollInterval * 1_000_000_000))
        }
    }

    private func pollOnce() async {
        guard let client = WidgetDataClient.makeIfConfigured(timeout: Self.pollInterval) else {
            // A deleted cluster must not leave its old alerts looking current.
            // The persisted state stays on disk, keyed by that cluster id.
            if !state.rows.isEmpty { state = AlertFeedState() }
            loadedClusterKey = nil
            return
        }

        let key = Self.clusterKey()
        if key != loadedClusterKey {
            loadedClusterKey = key
            state = Self.loadState(defaults: defaults, key: key) ?? AlertFeedState()
        }

        await refreshAuthorizationIfNeeded()

        guard let known = state.knownID else {
            await loadBaseline(client: client)
            return
        }

        switch await client.alertFeed(since: known, limit: 100) {
        case .failure:
            // Keep the last rows: a poll hiccup is not evidence the alerts are
            // gone, and the dropdown already renders their age.
            return
        case .success(let page):
            if AlertFeedPolicy.isFeedReset(page: page, knownID: known) {
                await loadBaseline(client: client)
                return
            }
            let advance = AlertFeedPolicy.advance(page: page, from: state)
            state = advance.state
            persist()
            deliver(advance)
        }
    }

    private func loadBaseline(client: WidgetDataClient) async {
        guard case .success(let page) = await client.alertFeed(since: nil, limit: 20) else {
            return
        }
        state = AlertFeedPolicy.baseline(page: page)
        persist()
    }

    // MARK: - Notification permission

    /// Ask once, then re-read the system state about once a minute so a grant
    /// or revoke in System Settings is reflected without a relaunch.
    private func refreshAuthorizationIfNeeded() async {
        if let last = lastAuthorizationCheck,
           Date().timeIntervalSince(last) < 60 {
            return
        }
        lastAuthorizationCheck = Date()

        let center = UNUserNotificationCenter.current()
        let settings = await center.notificationSettings()
        switch settings.authorizationStatus {
        case .notDetermined:
            guard !authorizationRequested else { return }
            authorizationRequested = true
            let granted = (try? await center.requestAuthorization(options: [.alert, .sound])) ?? false
            notificationsDenied = !granted
        case .authorized, .provisional:
            notificationsDenied = false
        default:
            notificationsDenied = true
        }
    }

    // MARK: - Delivery

    private func deliver(_ advance: AlertFeedAdvance) {
        guard advance.suppressed > 0 || !advance.notify.isEmpty else { return }
        guard !notificationsDenied else { return }

        if advance.suppressed > 0 {
            let plural = advance.suppressed == 1 ? "alert" : "alerts"
            post(identifier: "hscc.alerts.more.\(advance.state.knownID ?? 0)",
                 title: "\(advance.suppressed) earlier HSCC \(plural)",
                 body: "Open the menu bar for the full list.",
                 thread: "hscc.alerts.more")
        }
        for alert in advance.notify {
            let body = (alert.body ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            post(identifier: "hscc.alert.\(alert.id)",
                 title: alert.title.isEmpty ? "HSCC alert" : alert.title,
                 body: body,
                 thread: "hscc.alerts")
        }
    }

    private func post(identifier: String, title: String, body: String, thread: String) {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.sound = .default
        content.threadIdentifier = thread
        let request = UNNotificationRequest(identifier: identifier,
                                            content: content,
                                            trigger: nil)
        UNUserNotificationCenter.current().add(request, withCompletionHandler: nil)
    }

    // MARK: - Persistence

    private func persist() {
        guard let key = loadedClusterKey,
              let data = try? JSONEncoder().encode(state) else { return }
        defaults.set(data, forKey: Self.stateKey(key))
    }

    private static func loadState(defaults: UserDefaults, key: String) -> AlertFeedState? {
        guard let data = defaults.data(forKey: stateKey(key)) else { return nil }
        return try? JSONDecoder().decode(AlertFeedState.self, from: data)
    }

    private static func stateKey(_ clusterKey: String) -> String {
        "hscc.mac.alertFeed." + clusterKey
    }

    /// One cursor per saved cluster: ids from two clusters' feeds are unrelated,
    /// and comparing them would either skip every alert or replay old ones.
    private static func clusterKey() -> String {
        let suite = UserDefaults(suiteName: AppGroup.suiteName)
        let id = suite?.string(forKey: AppGroup.activeClusterIDKey) ?? ""
        return id.isEmpty ? "default" : id
    }
}

// MARK: - Foreground banners

extension AlertFeedMonitor: UNUserNotificationCenterDelegate {
    /// The app is running by definition when it polls; ask macOS to present the
    /// banner anyway instead of silently suppressing it for the active app.
    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .list, .sound])
    }
}
