import Foundation

// MARK: - In-app update feed (iOS)
//
// The release pipeline publishes https://pom11.github.io/hscc/ios/version.json
// (scripts/apps_release.py). This file is the pure half of the check: parsing,
// ordering and the dismissal rule. It is Foundation-only and compiled ONLY
// into the app target — never into the widget or Live Activity extensions —
// so an extension can never start an update check of its own.
//
// Nothing here talks to the cluster: the feed is the public GitHub Pages file,
// fetched by UpdateChecker over its own ephemeral URLSession with no account,
// no bearer token and no HSCCClient involvement.

/// A dotted numeric version plus the monotonic build number.
///
/// Ordering is numeric component by component (`1.0.9 < 1.0.10`), padded so
/// `1.1` and `1.1.0` are equal, with the build number as the tie-break when
/// the marketing versions match. The stored `version` is canonical (leading
/// `v` removed, components normalized), which is what the dismissal record
/// compares against.
struct AppVersion: Comparable, Equatable {
    let version: String
    let build: Int
    private let components: [Int]

    init?(version: String, build: Int) {
        var text = version.trimmingCharacters(in: .whitespacesAndNewlines)
        if text.hasPrefix("v") || text.hasPrefix("V") {
            text.removeFirst()
        }
        guard !text.isEmpty, build >= 0 else { return nil }
        let parts = text.split(separator: ".", omittingEmptySubsequences: false)
        guard !parts.isEmpty else { return nil }
        var numbers: [Int] = []
        for part in parts {
            guard let number = Int(part), number >= 0 else { return nil }
            numbers.append(number)
        }
        components = numbers
        self.version = numbers.map(String.init).joined(separator: ".")
        self.build = build
    }

    static func == (lhs: AppVersion, rhs: AppVersion) -> Bool {
        lhs.compare(to: rhs) == 0
    }

    static func < (lhs: AppVersion, rhs: AppVersion) -> Bool {
        lhs.compare(to: rhs) < 0
    }

    private func compare(to other: AppVersion) -> Int {
        let count = max(components.count, other.components.count)
        for index in 0..<count {
            let mine = index < components.count ? components[index] : 0
            let theirs = index < other.components.count ? other.components[index] : 0
            if mine != theirs { return mine < theirs ? -1 : 1 }
        }
        if build != other.build { return build < other.build ? -1 : 1 }
        return 0
    }
}

/// The decoded `version.json` contract. Every field is required: a feed with a
/// missing key, an unparseable version/build or a non-HTTPS link is rejected
/// whole, so a broken release feed can only mean "no banner", never a banner
/// pointing at something unverifiable.
struct UpdateFeed: Equatable {
    let appVersion: AppVersion
    let notesURL: URL
    let installURL: URL

    var version: String { appVersion.version }
    var build: Int { appVersion.build }

    init(version: String, build: Int, notesURL: URL, installURL: URL) throws {
        guard let appVersion = AppVersion(version: version, build: build), build > 0 else {
            throw UpdateFeedError.invalidVersion
        }
        guard Self.isSecure(notesURL), Self.isSecure(installURL) else {
            throw UpdateFeedError.insecureURL
        }
        self.appVersion = appVersion
        self.notesURL = notesURL
        self.installURL = installURL
    }

    /// Nil for malformed JSON, missing fields, an unorderable version or an
    /// insecure/unparseable URL. The caller treats nil as "no update".
    static func decode(_ data: Data) -> UpdateFeed? {
        guard let raw = try? JSONDecoder().decode(RawFeed.self, from: data),
              let notesURL = URL(string: raw.notes_url),
              let installURL = URL(string: raw.install_url) else {
            return nil
        }
        return try? UpdateFeed(version: raw.version,
                               build: raw.build,
                               notesURL: notesURL,
                               installURL: installURL)
    }

    private static func isSecure(_ url: URL) -> Bool {
        url.scheme?.lowercased() == "https" && url.host != nil
    }

    private struct RawFeed: Decodable {
        let version: String
        let build: Int
        let notes_url: String
        let install_url: String
    }
}

enum UpdateFeedError: Error {
    case invalidVersion
    case insecureURL
}

/// What the banner needs when the published release is newer than this app.
struct AvailableUpdate: Equatable, Identifiable {
    let version: String
    let build: Int
    let notesURL: URL
    let installURL: URL

    var id: String { version }
}

enum UpdateDecision {
    /// A newer release, regardless of whether the operator dismissed it.
    static func newerUpdate(feed: UpdateFeed, running: AppVersion) -> AvailableUpdate? {
        guard feed.appVersion > running else { return nil }
        return AvailableUpdate(version: feed.version,
                               build: feed.build,
                               notesURL: feed.notesURL,
                               installURL: feed.installURL)
    }

    /// The banner decision: newer than the running app AND not dismissed for
    /// THIS version. Dismissing 1.1.0 never hides 1.1.1.
    static func availableUpdate(feed: UpdateFeed,
                                running: AppVersion,
                                dismissedVersion: String?) -> AvailableUpdate? {
        guard let update = newerUpdate(feed: feed, running: running) else { return nil }
        if let dismissedVersion,
           canonical(dismissedVersion) == update.version {
            return nil
        }
        return update
    }

    private static func canonical(_ version: String) -> String {
        AppVersion(version: version, build: 0)?.version ?? version
    }
}

/// The app-local bookkeeping behind "at most every six hours" and "dismiss
/// remembers that version only". It lives in the app's own defaults, not the
/// App Group: no extension performs or displays an update check.
struct UpdatePreferences {
    static let checkInterval: TimeInterval = 6 * 60 * 60

    private enum Keys {
        static let lastAttempt = "hscc.ios.update.lastAttempt"
        static let lastResult = "hscc.ios.update.lastResult"
        static let dismissedVersion = "hscc.ios.update.dismissedVersion"
    }

    let defaults: UserDefaults

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
    }

    func lastAttempt() -> Date? {
        defaults.object(forKey: Keys.lastAttempt) as? Date
    }

    func recordAttempt(at date: Date) {
        defaults.set(date, forKey: Keys.lastAttempt)
    }

    func lastResult() -> String? {
        defaults.string(forKey: Keys.lastResult)
    }

    func recordResult(_ result: String) {
        defaults.set(result, forKey: Keys.lastResult)
    }

    func dismissedVersion() -> String? {
        defaults.string(forKey: Keys.dismissedVersion)
    }

    func dismiss(version: String) {
        defaults.set(version, forKey: Keys.dismissedVersion)
    }

    /// True on a fresh install, after six hours, and after the clock moved
    /// backwards (rather than waiting for a wall-clock interval that may be
    /// hours in the future).
    func isDue(at now: Date) -> Bool {
        guard let last = lastAttempt() else { return true }
        return now < last || now.timeIntervalSince(last) >= Self.checkInterval
    }
}
