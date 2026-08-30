import Foundation
import ActivityKit

// ---------------------------------------------------------------------------
// Live Activity — session activity.
//
// `SessionActivityAttributes` is the ActivityKit attributes type for the
// session Live Activity: it surfaces the LIVE activity of a project's session
// on the Lock Screen / Dynamic Island. It is compiled into BOTH the app (which
// starts/updates/ends the activity via `SessionActivityDriver`) and the
// HSCCLiveActivity extension (which renders it via an `ActivityConfiguration`),
// so the two sides agree on the exact activity type and its content state.
//
// Unlike the fleet-wake activity (a one-shot, ends on settle), this one is a
// LIVING mirror of the foreground project's streaming session: it updates
// continuously as session events fold, and ends when the operator leaves that
// project's live chat.
// ---------------------------------------------------------------------------

struct SessionActivityAttributes: ActivityAttributes {
    /// The state the Lock Screen / Dynamic Island renders. Carries exactly
    /// what the rendered surfaces draw — nothing more. `phase` is the coarse
    /// turn state ("streaming" / "tool" / "done" / "error" / "idle") that
    /// drives the glyph + color; `headline`/`detail` carry the human-readable
    /// summary the app derived from the session event stream.
    public struct ContentState: Codable, Hashable {
        /// The project whose session this mirrors.
        public var project: String
        /// Coarse turn state: "streaming" (assistant deltas landing) /
        /// "tool" (a tool call in flight) / "done" (a reply has settled) /
        /// "error" (a named failure) / "idle" (no activity yet).
        public var phase: String
        /// Headline line, e.g. "replying…", "tool: read_kanban", "reply ready".
        public var headline: String
        /// Short secondary line — the streaming text tail or last event context.
        public var detail: String
        /// Number of session events folded since this activity window began.
        public var activityCount: Int
        /// When the most recent activity was observed — the lock screen can
        /// show how long ago it went quiet.
        public var lastActivityAt: Date
    }

    /// A token identifying this session activity so it can be updated/ended.
    public var sessionID: UUID = UUID()
}
