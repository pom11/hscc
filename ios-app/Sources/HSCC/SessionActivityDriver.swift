import Foundation
import ActivityKit

// ---------------------------------------------------------------------------
// SessionActivityDriver — the APP side of the session Live Activity.
//
// This is the LIVE mirror of a project's streaming session on the Lock Screen
// / Dynamic Island. The streaming chat view calls `reflect(rows:phase:)` every
// time its composed rows or connection phase change; this driver lazily starts
// the activity on first live activity, updates it as events fold, and ends it
// when the operator leaves that project's live chat (or when there is nothing
// honest left to show).
//
// Separation of concerns:
//   * `SessionActivitySummary.make(rows:phase:)` — the PURE derivation of
//     phase/headline/detail from the folded rows (headless-testable, no
//     ActivityKit).
//   * this driver — the ActivityKit glue: maps a summary onto the shared
//     `SessionActivityAttributes.ContentState` and starts/updates/ends.
//
// Uses the CLASS-based ActivityKit API (`Activity.request`, then instance
// `update` / `end`), matching `LiveActivityManager`.
// ---------------------------------------------------------------------------

@MainActor
final class SessionActivityDriver {
    /// The in-flight session activity, if any. One per foreground project.
    private var current: Activity<SessionActivityAttributes>?

    /// The activity window started (for event counting) and the project whose
    /// session we mirror — ends/ignores when the project changes or the view
    /// goes away.
    private var project: String?
    private var activityCount = 0

    /// Reflect a new snapshot of the foreground project's live session.
    ///
    /// - Parameters:
    ///   - project: the project whose session this mirrors.
    ///   - rows: the composed chat rows (from StreamingChatStore).
    ///   - phase: the live connection phase (from StreamingChatStore).
    ///
    /// A nil summary (nothing live, nothing folded) ends a lingering activity.
    func reflect(project: String, rows: [ChatRow], phase: ConnectionPhase) {
        // A project change ends any prior activity so we never keep stale
        // content for a different project on screen.
        if self.project != nil && self.project != project {
            end()
            self.project = nil
        }
        self.project = project

        guard let summary = SessionActivitySummary.make(rows: rows, phase: phase,
                                                        activityCount: activityCount) else {
            // Nothing honest to mirror — drop any lingering activity.
            end()
            return
        }

        // Count events folded since this activity window began.
        activityCount = max(activityCount, rows.count)

        let content = SessionActivityAttributes.ContentState(
            project: project,
            phase: summary.phase,
            headline: summary.headline,
            detail: summary.detail,
            activityCount: summary.activityCount,
            lastActivityAt: Date())

        if current == nil {
            start(content)
        } else {
            Task { await current?.update(ActivityContent(state: content, staleDate: nil)) }
        }
    }

    /// End the activity (the operator left the live chat). Dismisses without a
    /// lingering bubble — a session mirror simply goes away when not watched.
    func end() {
        guard let current else { return }
        let activity = current
        self.current = nil
        Task {
            await activity.end(nil, dismissalPolicy: .immediate)
        }
    }

    private func start(_ content: SessionActivityAttributes.ContentState) {
        let attributes = SessionActivityAttributes()
        do {
            current = try Activity.request(
                attributes: attributes,
                content: ActivityContent(state: content, staleDate: nil),
                pushType: nil)
        } catch {
            // The system refused to present a Live Activity — nothing to do.
            // The in-app chat still shows everything; this is peripheral.
            current = nil
        }
    }
}
