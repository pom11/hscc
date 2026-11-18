import Foundation
import SwiftUI

/// The app half of the in-app update check.
///
/// The feed is the PUBLIC GitHub Pages JSON the release pipeline writes:
///
///     https://pom11.github.io/hscc/ios/version.json
///
/// It is fetched by its own ephemeral `URLSession` with a short timeout, no
/// cache, no bearer token and no `HSCCClient` — the cluster connection is never
/// involved, so an unreachable cluster cannot block (or fake) an update check.
///
/// Timing: `checkIfDue()` is safe to call at launch and on every foreground
/// entry; `UpdatePreferences` caps it at one ATTEMPT per six hours, including
/// failed attempts. The explicit "Check for updates" button uses `checkNow()`
/// and is deliberately not throttled.
@MainActor
final class UpdateChecker: ObservableObject {
    static let feedURL = URL(string: "https://pom11.github.io/hscc/ios/version.json")!
    private static let requestTimeout: TimeInterval = 8
    private static let maximumFeedBytes = 256 * 1024

    @Published private(set) var available: AvailableUpdate?
    @Published private(set) var lastResult: String?
    @Published private(set) var lastChecked: Date?
    @Published private(set) var isChecking = false
    @Published private(set) var dismissedVersion: String?

    let runningVersion: AppVersion

    private let preferences: UpdatePreferences
    private let session: URLSession
    private let now: () -> Date

    init(preferences: UpdatePreferences = UpdatePreferences(),
         session: URLSession? = nil,
         now: @escaping () -> Date = Date.init,
         runningVersion: AppVersion? = nil) {
        self.preferences = preferences
        self.now = now
        self.runningVersion = runningVersion ?? Self.bundleVersion()
        self.lastResult = preferences.lastResult()
        self.lastChecked = preferences.lastAttempt()
        self.dismissedVersion = preferences.dismissedVersion()
        if let session {
            self.session = session
        } else {
            let configuration = URLSessionConfiguration.ephemeral
            configuration.timeoutIntervalForRequest = Self.requestTimeout
            configuration.timeoutIntervalForResource = Self.requestTimeout * 2
            configuration.requestCachePolicy = .reloadIgnoringLocalAndRemoteCacheData
            self.session = URLSession(configuration: configuration)
        }
    }

    /// The banner's input: nil when there is no newer release or the operator
    /// dismissed exactly this version.
    var bannerUpdate: AvailableUpdate? {
        guard let available else { return nil }
        return available.version == dismissedVersion ? nil : available
    }

    /// Launch/foreground check, at most once per six hours (attempts included).
    func checkIfDue() async {
        guard preferences.isDue(at: now()) else { return }
        await performCheck()
    }

    /// The explicit Settings action. Not throttled, but still one request at a
    /// time.
    func checkNow() async {
        await performCheck()
    }

    func dismiss(_ update: AvailableUpdate) {
        dismissedVersion = update.version
        preferences.dismiss(version: update.version)
    }

    private func performCheck() async {
        guard !isChecking else { return }
        isChecking = true
        let attempt = now()
        lastChecked = attempt
        // Record the ATTEMPT before the network call: a timeout or a 500 must
        // also wait six hours, otherwise every foreground entry becomes one.
        preferences.recordAttempt(at: attempt)
        defer { isChecking = false }

        do {
            var request = URLRequest(url: Self.feedURL,
                                     cachePolicy: .reloadIgnoringLocalAndRemoteCacheData,
                                     timeoutInterval: Self.requestTimeout)
            request.httpMethod = "GET"
            request.setValue("application/json", forHTTPHeaderField: "Accept")
            // No Authorization header and no cluster URL by construction.
            let (data, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse,
                  (200..<300).contains(http.statusCode),
                  data.count <= Self.maximumFeedBytes,
                  let feed = UpdateFeed.decode(data) else {
                throw UpdateCheckError.badFeed
            }

            let update = UpdateDecision.newerUpdate(feed: feed, running: runningVersion)
            available = update
            let result: String
            if let update {
                result = "Update \(update.version) available (build \(update.build))."
            } else {
                result = "Up to date — \(runningVersion.version) (build \(runningVersion.build)) is current."
            }
            lastResult = result
            preferences.recordResult(result)
        } catch {
            available = nil
            let result = "Couldn't check for updates."
            lastResult = result
            preferences.recordResult(result)
        }
    }

    private static func bundleVersion() -> AppVersion {
        let info = Bundle.main.infoDictionary
        let version = info?["CFBundleShortVersionString"] as? String ?? "0.0.0"
        let buildText = info?["CFBundleVersion"] as? String ?? "0"
        let build = Int(buildText) ?? 0
        return AppVersion(version: version, build: build)
            ?? AppVersion(version: "0.0.0", build: 0)!
    }

    private enum UpdateCheckError: Error {
        case badFeed
    }
}
