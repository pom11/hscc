import Foundation

// ===========================================================================
// SessionActivitySummary — the pure derivation behind the session Live
// Activity.
//
// The session Live Activity is a LIVING mirror of a project's streaming
// session on the Lock Screen / Dynamic Island. The app drives it from the SAME
// composed rows + live phase that feed the in-app streaming chat
// (StreamingTranscript / StreamingChatStore) — so what the lock screen shows is
// exactly what the operator would see if they looked at the chat. No invented
// progress: this derives the phase, headline and detail from the actual last
// folded row and the live connection state.
//
// This file is deliberately pure Foundation (NO ActivityKit, NO SwiftUI, NO
// network) so it can be proven headlessly against real rows — the same
// "no iOS runtime on this host" rule as streaming_check.sh. The ActivityKit
// wrapper (`SessionActivityDriver`) maps a `SessionActivitySummary` onto the
// shared `SessionActivityAttributes.ContentState`; the derivation itself does
// not need to know about ActivityKit at all.
// ===========================================================================

/// A compact snapshot of a project's live session activity, ready to render on
/// a peripheral surface. Pure value: every field is derived from the folded
/// rows + live phase handed in.
struct SessionActivitySummary {
    /// Coarse turn state: "streaming" / "tool" / "done" / "error" / "idle".
    let phase: String
    /// Headline line, e.g. "replying…", "tool: read_kanban", "reply ready".
    let headline: String
    /// Short secondary line — streaming text tail or last event context.
    let detail: String
    /// Event count folded since the activity window began.
    let activityCount: Int

    /// Derive a summary from the composed rows + live phase.
    ///
    /// Returns nil when there is nothing honest to show — no rows and no live
    /// connection — so the caller can drop the activity rather than presenting
    /// an empty bubble.
    static func make(rows: [ChatRow],
                     phase: ConnectionPhase,
                     activityCount: Int = 0) -> SessionActivitySummary? {
        // Not live and nothing folded yet → nothing to mirror.
        guard !rows.isEmpty || phase.isLive else { return nil }

        // The last folded row drives the headline (most recent activity wins).
        if let last = rows.last {
            switch last.item {
            case .message(let role, let text, let streaming):
                if streaming {
                    return SessionActivitySummary(
                        phase: "streaming",
                        headline: "replying…",
                        detail: textTail(text),
                        activityCount: activityCount)
                }
                // A settled assistant message = a reply is ready.
                if role == "user" {
                    return SessionActivitySummary(
                        phase: "done",
                        headline: "said",
                        detail: textTail(text),
                        activityCount: activityCount)
                }
                return SessionActivitySummary(
                    phase: "done",
                    headline: "reply ready",
                    detail: textTail(text),
                    activityCount: activityCount)

            case .tool(let t):
                if t.finished {
                    return SessionActivitySummary(
                        phase: "tool",
                        headline: "tool: \(t.name)",
                        detail: toolDetail(t),
                        activityCount: activityCount)
                }
                // In-flight tool call.
                return SessionActivitySummary(
                    phase: "tool",
                    headline: "tool: \(t.name)",
                    detail: "running…",
                    activityCount: activityCount)

            case .card(let p):
                return SessionActivitySummary(
                    phase: "done",
                    headline: "card \(p.status)",
                    detail: p.title,
                    activityCount: activityCount)

            case .agent(let p):
                let action = p.isSpawned ? "spawned" : "finished"
                return SessionActivitySummary(
                    phase: "tool",
                    headline: "agent \(p.role)",
                    detail: "\(action)\(p.task.map { " — \($0)" } ?? "")",
                    activityCount: activityCount)

            case .system(let p):
                return SessionActivitySummary(
                    phase: "done",
                    headline: p.kind,
                    detail: systemDetail(p),
                    activityCount: activityCount)

            case .error(let p):
                return SessionActivitySummary(
                    phase: "error",
                    headline: "error",
                    detail: p.message,
                    activityCount: activityCount)

            case .notice(let text), .unknown(_, let text):
                return SessionActivitySummary(
                    phase: "done",
                    headline: "notice",
                    detail: textTail(text),
                    activityCount: activityCount)
            }
        }

        // Live + connected but nothing folded yet — honest "watching".
        return SessionActivitySummary(
            phase: "idle",
            headline: "listening…",
            detail: "no activity yet",
            activityCount: activityCount)
    }

    /// A one-line tail of a message for the lock screen (which has little
    /// room) — collapse a long stream to its last ~80 characters.
    private static func textTail(_ text: String) -> String {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.count > 80 else { return trimmed }
        return "…" + String(trimmed.suffix(80))
    }

    private static func toolDetail(_ t: ToolRender) -> String {
        if let args = t.args, !args.isEmpty {
            return args.renderArgs(pretty: false)
        }
        return t.finished ? "done" : "running…"
    }

    private static func systemDetail(_ p: SystemPayload) -> String {
        guard let details = p.details else { return p.kind }
        return details.renderArgs(pretty: false)
    }
}
