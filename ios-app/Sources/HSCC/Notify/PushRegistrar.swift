import Combine
import Foundation
import UIKit
import UserNotifications

// ===========================================================================
// PushRegistrar — the APNs half of operator alerts (card N3).
//
// The notification DECISION (NeedsOperatorNotifier / OperatorAlert) and the
// foreground local-banner path stay exactly as they were. This type adds the
// transport that reaches the phone while the app is closed:
//
//   1. the app delegate asks iOS for a device token
//      (`registerForRemoteNotifications`), only once notification permission
//      has been granted;
//   2. iOS answers with the token (`didRegisterForRemoteNotificationsWith…`);
//   3. the token is POSTed to /v1/devices on EVERY saved cluster, through the
//      normal HSCCClient — so the cluster's failover address list and bearer
//      token rules apply unchanged. Registration is an idempotent refresh on
//      the server (N2), which is why doing it on every launch/foreground is
//      safe and is also how a sandbox -> production move corrects itself;
//   4. the daemon's APNs sender (N2) then pushes each new alert in the feed.
//
// The token is a device identifier, not a secret, but it is still runtime
// state: it lives in the App-Group defaults on this phone and is never logged,
// rendered in Settings, or written to the repo. Settings shows the derived
// registration status instead.
//
// `aps-environment` is production in Release (the Ad Hoc build) and
// development in Debug; the provisioning profile must carry the same value.
// See ios-app/RELEASE.md "Push notifications".
// ===========================================================================

struct PushDeviceRegistration: Decodable {
    struct Device: Decodable {
        let id: String
        let platform: String?
        let bundle_id: String?
        let env: String?
    }

    let device: Device
    let created: Bool
    let count: Int
    let speak: String?
}

struct PushTestResult: Decodable {
    let sent: Bool
    let sent_count: Int?
    let reason: String?
    let detail: String?
    let speak: String?
}

@MainActor
final class PushRegistrar: ObservableObject {
    static let shared = PushRegistrar()

    /// What Settings tells the operator. Deliberately says WHY push is not
    /// working instead of a bare "off": permission, missing token, or the last
    /// registration failure are three different fixes.
    enum Status: Equatable {
        case unknown
        case denied
        case awaitingToken
        case registering
        case registered(clusters: Int)
        case failed(String)

        var text: String {
            switch self {
            case .unknown:
                return "Waiting for permission"
            case .denied:
                return "Off in iOS Settings"
            case .awaitingToken:
                return "Waiting for Apple's device token"
            case .registering:
                return "Registering with the cluster…"
            case .registered(let clusters):
                return clusters == 1
                    ? "Push enabled — registered with 1 cluster"
                    : "Push enabled — registered with \(clusters) clusters"
            case .failed(let message):
                return "Not registered — \(message)"
            }
        }
    }

    @Published private(set) var status: Status = .unknown
    /// Mirrors the notification permission. Settings uses it to explain the
    /// toggles; the delegate only asks APNs for a token when this is true.
    @Published private(set) var authorized = false
    /// The raw token is kept only to re-register on foreground/cluster edits.
    /// It is never displayed or logged.
    @Published private(set) var deviceToken: String?

    private let defaults: UserDefaults
    private var registrationTask: Task<Void, Never>?

    private static let tokenKey = "hscc.push.deviceToken"

    private init() {
        defaults = UserDefaults(suiteName: AppGroup.suiteName) ?? .standard
        deviceToken = defaults.string(forKey: Self.tokenKey)
    }

    // MARK: - Permission + APNs token

    /// Re-read the permission state. Called when the app becomes active so a
    /// grant/revoke in iOS Settings is picked up without a relaunch.
    func activate() async {
        let settings = await UNUserNotificationCenter.current().notificationSettings()
        // Before the first prompt is answered, leave the status alone: showing
        // "off in iOS Settings" while the system alert is still on screen would
        // be false. requestAuthorization reports the answer.
        guard settings.authorizationStatus != .notDetermined else { return }
        let granted = settings.authorizationStatus == .authorized
            || settings.authorizationStatus == .provisional
        setAuthorization(granted)
    }

    /// Called by NotificationCoordinator after the launch permission request.
    func setAuthorization(_ granted: Bool) {
        authorized = granted
        guard granted else {
            registrationTask?.cancel()
            registrationTask = nil
            status = .denied
            return
        }
        if deviceToken == nil, status != .registering {
            status = .awaitingToken
        }
        // Apple requires this on every launch once permission is granted; it
        // may answer with the same token (the registration POST is idempotent)
        // or a new one after a restore.
        UIApplication.shared.registerForRemoteNotifications()
        scheduleRegistration()
    }

    /// `application(_:didRegisterForRemoteNotificationsWithDeviceToken:)`.
    func didRegister(deviceToken data: Data) {
        let token = data.map { String(format: "%02x", $0) }.joined()
        guard !token.isEmpty else { return }
        deviceToken = token
        defaults.set(token, forKey: Self.tokenKey)
        status = .registering
        scheduleRegistration()
    }

    /// `application(_:didFailToRegisterForRemoteNotificationsWithError:)`.
    /// The message names the fix (capability/profile), never the token.
    func didFailToRegister(_ error: Error) {
        status = .failed(error.localizedDescription)
    }

    /// The saved-cluster list changed (add/edit/delete): push the token to the
    /// new set. A no-op until permission and a token exist.
    func clusterListChanged() {
        scheduleRegistration()
    }

    private func scheduleRegistration() {
        guard authorized, deviceToken != nil else { return }
        registrationTask?.cancel()
        registrationTask = Task { @MainActor [weak self] in
            await self?.registerWithAllClusters()
        }
    }

    // MARK: - Registration

    /// POST the token to EVERY saved cluster. Each cluster can have several
    /// addresses; HSCCClient tries them in failover order. One unreachable
    /// cluster never stops the others from being registered.
    private func registerWithAllClusters() async {
        guard authorized else {
            status = .denied
            return
        }
        guard let token = deviceToken else {
            status = .awaitingToken
            return
        }

        let clusters = Self.savedClusters()
        guard !clusters.isEmpty else {
            status = .failed("no saved cluster")
            return
        }

        status = .registering
        var registered = 0
        var firstFailure: String?

        for cluster in clusters {
            guard let apiToken = KeychainStore.readToken(forCluster: cluster.id),
                  !apiToken.isEmpty else { continue }
            let client = HSCCClient(host: cluster.host,
                                    fallbacks: cluster.fallbackHosts,
                                    port: cluster.port,
                                    token: apiToken,
                                    monitorsConnection: false)
            do {
                _ = try await client.registerPushDevice(
                    token: token,
                    bundleID: Self.bundleID,
                    environment: Self.apnsEnvironment)
                registered += 1
            } catch {
                if firstFailure == nil { firstFailure = error.localizedDescription }
            }
        }

        if Task.isCancelled { return }
        guard authorized else {
            status = .denied
            return
        }
        if registered > 0 {
            status = .registered(clusters: registered)
        } else if let firstFailure {
            status = .failed(firstFailure)
        } else {
            status = .failed("no saved cluster with a token")
        }
    }

    // MARK: - Settings "Send test"

    /// Refresh this device on the ACTIVE cluster, then ask it for the same test
    /// push `hscc notify test` sends (POST /v1/devices/test). Returns an
    /// operator-readable line for Settings — never throws into the view.
    func sendTestPush() async -> String {
        guard authorized else {
            return "Notifications are off in iOS Settings — enable them for HSCC there."
        }
        guard let token = deviceToken else {
            return "Waiting for Apple's device token — try again in a moment."
        }
        guard let cluster = Self.activeCluster() else {
            return "Save a cluster with a token first."
        }
        guard let apiToken = KeychainStore.readToken(forCluster: cluster.id),
              !apiToken.isEmpty else {
            return "The active cluster has no token saved."
        }

        let client = HSCCClient(host: cluster.host,
                                fallbacks: cluster.fallbackHosts,
                                port: cluster.port,
                                token: apiToken,
                                monitorsConnection: false)
        do {
            // Make sure this phone is in the roster before asking for a push;
            // a fresh install can press Send test before the launch
            // registration round-trip has finished.
            _ = try await client.registerPushDevice(
                token: token,
                bundleID: Self.bundleID,
                environment: Self.apnsEnvironment)
            let result = try await client.sendTestPush()
            if result.sent {
                return result.speak ?? "Test push sent."
            }
            return result.speak ?? "Test push not sent (\(result.reason ?? "unknown"))."
        } catch {
            return error.localizedDescription
        }
    }

    // MARK: - Runtime state readers

    private static var suite: UserDefaults {
        UserDefaults(suiteName: AppGroup.suiteName) ?? .standard
    }

    private static func savedClusters() -> [SavedCluster] {
        guard let raw = suite.data(forKey: AppGroup.clustersKey),
              let clusters = try? JSONDecoder().decode([SavedCluster].self, from: raw) else {
            return []
        }
        return clusters
    }

    private static func activeCluster() -> SavedCluster? {
        let clusters = savedClusters()
        guard let raw = suite.string(forKey: AppGroup.activeClusterIDKey),
              let id = UUID(uuidString: raw),
              let active = clusters.first(where: { $0.id == id }) else {
            return clusters.first
        }
        return active
    }

    private static var bundleID: String {
        Bundle.main.bundleIdentifier ?? ""
    }

    /// Debug builds sign with a development profile; the Ad Hoc Release build
    /// signs with a distribution profile and must register `production`.
    private static var apnsEnvironment: String {
        #if DEBUG
        return "development"
        #else
        return "production"
        #endif
    }
}
