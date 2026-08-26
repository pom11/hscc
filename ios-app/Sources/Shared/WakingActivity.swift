import Foundation
import ActivityKit

// ---------------------------------------------------------------------------
// Live Activity — fleet wake progress.
//
// `WakingActivityAttributes` is the ActivityKit attributes type for the wake
// Live Activity. It is compiled into BOTH the app (which starts/updates/ends
// the activity via `LiveActivityManager`) and the HSCCLiveActivity extension
// (which renders it via `ActivityConfiguration`), so the two sides agree on
// the exact activity type and its content state.
// ---------------------------------------------------------------------------

struct WakingActivityAttributes: ActivityAttributes {
    /// Purely structural type — a wake is identified by `state`. Empty in
    /// practice; kept as a struct so the attributes conform to the protocol.
    public struct ContentState: Codable, Hashable {
        /// The autodown cluster state string ("waking" / "up" / "down").
        public var state: String
        /// Per-node readiness as an ordered list of `.IP-number` labels whose
        /// nodes are currently serving. Honest progress — only what's known.
        public var upNodes: [String]
        /// The time the wake began. The view counts elapsed from this; there is
        /// no reliable progress signal, so elapsed time is the honest metric.
        public var startedAt: Date?
        /// True when the wake finished successfully (state → up).
        public var succeeded: Bool
        /// True when the wake failed (state → down / other) — the activity ends
        /// and must say so rather than just disappearing.
        public var failed: Bool
        /// Optional failure/settled message shown when the activity ends.
        public var message: String?
    }

    /// A token identifying this wake so the activity can be ended/unmatched.
    public var wakeID: UUID = UUID()
}
