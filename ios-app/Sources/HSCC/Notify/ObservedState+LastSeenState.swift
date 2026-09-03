import Foundation

// ===========================================================================
// ObservedState / LastSeenState — the differential-notification state model.
//
// The plan (docs/notify-operator-plan.md) requires detection to be
// DIFFERENTIAL, not absolute: fire only when a condition *first* appears, and
// dedup so the same occurrence is never re-announced. That state lives here.
//
// Three types, all Codable + Equatable so they round-trip through JSON and are
// trivially unit-testable headlessly:
//   * ObservedState  — one snapshot of the polled endpoints (the "now").
//   * AnnouncedState — per-condition fingerprints already announced, so a
//     condition announces once per distinct occurrence.
//   * LastSeenState  — the full "prior" handed to `compute`: what we last saw
//     plus what we've already told the operator. This is what the plan calls
//     the prior in `NeedsOperatorNotifier.compute(prior:now:)`.
//
// Persistence: the coordinator writes `observed` under `hscc.notify.lastSeen`
// and `announced` under `hscc.notify.announced`, both in the shared App-Group
// suite (same suite the widgets / Live Activities read), so foreground and
// background share ONE store and never double-announce across each other.
// ===========================================================================

/// One snapshot of the polled endpoints — the "now" observation fed to
/// `NeedsOperatorNotifier.compute(prior:now:)`.
///
/// Sets are used (not arrays) so set-difference dedup is natural and order
/// never matters. Optional Booleans encode "we do not know this poll":
/// `apiReachable` is nil when the poll failed inconclusively (a transient
/// hiccup is NOT a confident "down"), distinct from a confident `false`.
struct ObservedState: Codable, Equatable {
    /// Review-queue ids currently awaiting review.
    var reviewQueue = Set<String>()
    /// Blocked-card ids currently blocked across all boards.
    var blocked = Set<String>()
    /// Number of pending escalations.
    var escalationsCount = 0
    /// nil = couldn't reach the API this poll (inconclusive, NOT a confident down).
    var apiReachable: Bool?
    /// Daemon running state, when the daemon-status poll succeeded.
    var daemonRunning: Bool?
}

/// Per-condition "what we have already announced" fingerprints.
///
/// The general dedup rule (from the plan):
///   * a condition announces when it is present now AND its fingerprint differs
///     from the last-announced one; after announcing we record the current
///     fingerprint;
///   * when the condition clears we forget the fingerprint, so a LATER
///     recurrence announces again.
/// Here each fingerprint IS the last-announced value for that condition
/// (the ids we told the operator about, the escalation count level reached,
/// whether we've already blared "fleet down").
struct AnnouncedState: Codable, Equatable {
    /// Review ids already announced — so a new id re-announces but an
    /// unchanged queue stays silent.
    var review = Set<String>()
    /// Blocked ids already announced, same semantics as review.
    var blocked = Set<String>()
    /// The escalation-count level already announced (0 = none).
    var escalations = 0
    /// Whether the current fleet-down episode has already been announced.
    var unreachable = false
}

/// The full "prior" the decision engine reads: last-observed values plus the
/// announced fingerprints. Encapsulates both persistence keys.
struct LastSeenState: Codable, Equatable {
    var observed = ObservedState()
    var announced = AnnouncedState()
    /// True once ANY poll has been persisted. The very first observation on a
    /// fresh install anchors silently (matches StreamReplyWatcher's "older
    /// history is prior art, never a wall of badges" precedent) — pre-existing
    /// conditions are not announced as if they just appeared.
    var observedOnce = false

    /// An empty prior — used for the very first poll on a fresh install.
    static let empty = LastSeenState()
}

/// Load/save `LastSeenState` in the shared App-Group suite.
///
/// `observed` is persisted under `hscc.notify.lastSeen`; `announced` under
/// `hscc.notify.announced`; `observedOnce` under `hscc.notify.observedOnce`.
/// Corrupt or missing data degrades to an empty state (mirrors
/// SettingsStore:76's tolerant decode) — the app never crashes and never
/// invents conditions because a value was unreadable.
enum LastSeenStateStore {
    static let lastSeenKey = "hscc.notify.lastSeen"
    static let announcedKey = "hscc.notify.announced"
    static let observedOnceKey = "hscc.notify.observedOnce"

    private static var suite: UserDefaults {
        UserDefaults(suiteName: AppGroup.suiteName) ?? .standard
    }

    /// Load the persisted prior, tolerating corrupt/missing data.
    static func load() -> LastSeenState {
        let d = suite
        var state = LastSeenState.empty
        if let raw = d.data(forKey: lastSeenKey),
           let decoded = try? JSONDecoder().decode(ObservedState.self, from: raw) {
            state.observed = decoded
        }
        if let raw = d.data(forKey: announcedKey),
           let decoded = try? JSONDecoder().decode(AnnouncedState.self, from: raw) {
            state.announced = decoded
        }
        // observedOnce defaults to false (missing) → the very first poll on a
        // truly fresh install anchors silently; a persisted true survives across
        // launches so a change since the last poll still fires.
        state.observedOnce = d.bool(forKey: observedOnceKey)
        return state
    }

    /// Persist the observed, announced, and observedOnce halves under their own
    /// keys.
    static func save(_ state: LastSeenState) {
        let d = suite
        if let data = try? JSONEncoder().encode(state.observed) {
            d.set(data, forKey: lastSeenKey)
        }
        if let data = try? JSONEncoder().encode(state.announced) {
            d.set(data, forKey: announcedKey)
        }
        d.set(state.observedOnce, forKey: observedOnceKey)
    }
}
