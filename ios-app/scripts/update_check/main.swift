import Foundation

// update_check — the pure iOS update-feed contract (t_2a1e5913).
//
// Compiles the REAL Sources/HSCC/UpdateCheck.swift (never redeclared here) into
// a plain macOS CLI. The network half (UpdateChecker.swift) is not exercised:
// no socket is opened, and the feed parsing/order/dismissal rules are pure.

var assertions = 0
var failures = 0

func check(_ condition: @autoclosure () -> Bool, _ label: String) {
    assertions += 1
    if condition() {
        print("PASS: \(label)")
    } else {
        failures += 1
        print("FAIL: \(label)")
    }
}

func feedJSON(version: String? = "1.1.0",
              build: Any? = 2,
              notes: String? = "https://pom11.github.io/hscc/ios/notes/1.1.0.html",
              install: String? = "https://pom11.github.io/hscc/ios/") -> Data {
    var object: [String: Any] = [
        "released_at": "2026-11-18T10:00:00Z",
        "min_os": "26.0",
    ]
    if let version { object["version"] = version }
    if let build { object["build"] = build }
    if let notes { object["notes_url"] = notes }
    if let install { object["install_url"] = install }
    return try! JSONSerialization.data(withJSONObject: object)
}

// ── Version ordering ────────────────────────────────────────────────────────

let v109 = AppVersion(version: "1.0.9", build: 1)!
let v1010 = AppVersion(version: "1.0.10", build: 1)!
check(v109 < v1010, "1.0.9 sorts before 1.0.10 (numeric, not lexical)")
check(v1010 > v109, "1.0.10 sorts after 1.0.9")
check(AppVersion(version: "2.0", build: 5)! == AppVersion(version: "2.0.0", build: 5)!,
      "2.0 and 2.0.0 compare equal (zero padding)")
check(AppVersion(version: "v1.2.3", build: 5)!.version == "1.2.3",
      "a leading v is removed from the canonical version")
check(AppVersion(version: "1.2.3", build: 6)! > AppVersion(version: "1.2.3", build: 5)!,
      "build is the tie-break when the marketing versions are equal")
check(AppVersion(version: "1.2.3", build: 5)! == AppVersion(version: "1.2.3", build: 5)!,
      "same version and build compare equal")
check(AppVersion(version: "banana", build: 1) == nil, "a non-numeric version is not orderable")
check(AppVersion(version: "1.2.3", build: -1) == nil, "a negative build is not orderable")

// ── Feed decoding ──────────────────────────────────────────────────────────

guard let validFeed = UpdateFeed.decode(feedJSON()) else {
    print("FAIL: the canonical version.json did not decode")
    exit(1)
}
check(validFeed.version == "1.1.0" && validFeed.build == 2,
      "valid feed decodes version and build")
check(validFeed.notesURL.absoluteString.hasSuffix("/ios/notes/1.1.0.html"),
      "valid feed keeps notes_url")
check(validFeed.installURL.absoluteString == "https://pom11.github.io/hscc/ios/",
      "valid feed keeps install_url")

check(UpdateFeed.decode(feedJSON(version: nil)) == nil,
      "missing version -> no feed (no banner)")
check(UpdateFeed.decode(feedJSON(build: nil)) == nil,
      "missing build -> no feed (no banner)")
check(UpdateFeed.decode(feedJSON(notes: nil)) == nil,
      "missing notes_url -> no feed (no banner)")
check(UpdateFeed.decode(feedJSON(install: nil)) == nil,
      "missing install_url -> no feed (no banner)")
check(UpdateFeed.decode(Data("not json at all".utf8)) == nil,
      "garbage JSON -> no feed (no banner)")
check(UpdateFeed.decode(feedJSON(build: "2")) == nil,
      "a non-integer build -> no feed (no banner)")
check(UpdateFeed.decode(feedJSON(build: 0)) == nil,
      "build 0 -> no feed (no banner)")
check(UpdateFeed.decode(feedJSON(version: "banana")) == nil,
      "an unorderable version -> no feed (no banner)")
check(UpdateFeed.decode(feedJSON(install: "http://pom11.github.io/hscc/ios/")) == nil,
      "a non-HTTPS install_url -> no feed (no banner)")
check(UpdateFeed.decode(feedJSON(notes: "not a url")) == nil,
      "an unparseable notes_url -> no feed (no banner)")

// ── Newer/dismissed decisions ──────────────────────────────────────────────

let running = AppVersion(version: "1.0.0", build: 1)!
guard let update = UpdateDecision.availableUpdate(feed: validFeed,
                                                  running: running,
                                                  dismissedVersion: nil) else {
    print("FAIL: a newer feed produced no update")
    exit(1)
}
check(update.version == "1.1.0" && update.build == 2,
      "a newer feed produces the banner update")
check(UpdateDecision.availableUpdate(feed: validFeed,
                                     running: AppVersion(version: "1.1.0", build: 2)!,
                                     dismissedVersion: nil) == nil,
      "an equal version+build produces no update")
check(UpdateDecision.availableUpdate(feed: validFeed,
                                     running: AppVersion(version: "1.1.0", build: 1)!,
                                     dismissedVersion: nil) != nil,
      "an equal version with a newer build produces an update")
check(UpdateDecision.availableUpdate(feed: validFeed,
                                     running: AppVersion(version: "1.1.1", build: 1)!,
                                     dismissedVersion: nil) == nil,
      "a running app newer than the feed produces no update")
check(UpdateDecision.availableUpdate(feed: validFeed,
                                     running: running,
                                     dismissedVersion: "1.1.0") == nil,
      "dismissing 1.1.0 hides exactly that version")
check(UpdateDecision.availableUpdate(feed: validFeed,
                                     running: running,
                                     dismissedVersion: "1.0.9") != nil,
      "dismissing an older version does not hide 1.1.0")

guard let nextFeed = UpdateFeed.decode(feedJSON(version: "1.1.1", build: 3)) else {
    print("FAIL: the follow-up feed did not decode")
    exit(1)
}
check(UpdateDecision.availableUpdate(feed: nextFeed,
                                     running: running,
                                     dismissedVersion: "1.1.0") != nil,
      "a dismissed version never hides a later release")

// ── Six-hour throttle + dismissal persistence ──────────────────────────────

let suite = "hscc.update-check.tests.\(UUID().uuidString)"
let defaults = UserDefaults(suiteName: suite)!
defer { defaults.removePersistentDomain(forName: suite) }
let preferences = UpdatePreferences(defaults: defaults)
let now = Date(timeIntervalSince1970: 1_700_000_000)

check(preferences.isDue(at: now), "a fresh install is due immediately")
preferences.recordAttempt(at: now)
check(!preferences.isDue(at: now.addingTimeInterval(5 * 60 * 60)),
      "a check five hours ago is not due")
check(preferences.isDue(at: now.addingTimeInterval(6 * 60 * 60)),
      "a check six hours ago is due")
check(preferences.isDue(at: now.addingTimeInterval(-60)),
      "a backwards clock is treated as due instead of waiting hours")

preferences.recordResult("Up to date — 1.0.0 (build 1) is current.")
check(preferences.lastResult()?.hasPrefix("Up to date") == true,
      "the last check result survives into the About section")
preferences.dismiss(version: update.version)
check(preferences.dismissedVersion() == "1.1.0",
      "dismissal stores exactly the offered version")
preferences.dismiss(version: "1.1.1")
check(preferences.dismissedVersion() == "1.1.1",
      "a later dismissal replaces the old one rather than accumulating")

if failures > 0 {
    print("\(failures) of \(assertions) assertions FAILED")
    exit(1)
}
print("PASS: \(assertions) assertions — update feed, ordering, dismissal and throttle")
