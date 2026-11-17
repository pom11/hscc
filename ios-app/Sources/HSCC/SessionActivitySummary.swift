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

// ===========================================================================
// The row vocabulary the session Live Activity still consumes.
//
// Relocated here by t_bd6e7c0a (iOS pivot: the app stops being an operator
// console). These declarations used to sit in the streaming chat pipeline —
// `StreamingTranscript.swift` (ToolRender / ChatItem / ChatRow, plus the two
// JSONValue rendering helpers) and `Views/StreamingChatStore.swift` (StreamPhase
// and its `ConnectionPhase` alias). Those files are deleted by this card, but
// `SessionActivitySummary.make(rows:phase:)` and
// `SessionActivityDriver.reflect(rows:phase:)` belong to the Live Activity path
// the pivot KEEPS, and they take these types as their input contract.
//
// They are MOVED, not changed: every declaration below is byte-identical to
// what it was in the deleted files, so the summary and the driver derive
// exactly what they derived before. They stay pure Foundation (no ActivityKit,
// no SwiftUI, no network) for the same reason this file always has — it is the
// half that can be reasoned about without an iOS runtime.
//
// `StreamingChatStore`'s `TurnState` and `StreamSettings` did NOT move: they
// existed only to drive the chat view's own working/approval banners, and
// nothing else reads them.
// ===========================================================================

/// The live connection state, surfaced in a banner so the operator always
/// knows whether they are seeing a LIVE stream or a static (possibly stale)
/// transcript.
enum StreamPhase: Equatable {
    case idle            // never attempted
    case loadingHistory  // fetching the seed page
    case connecting      // opening the WebSocket
    case connected       // live (replay tail folded, now streaming)
    case reconnecting    // socket dropped, retrying with resume
    case failed(String)  // could not connect; honest reason

    /// True when the operator is seeing LIVE updates (or the attempt to get
    /// there is in progress) — as opposed to a parked/stale transcript.
    var isLive: Bool { self == .connected || self == .reconnecting }
}

typealias ConnectionPhase = StreamPhase

/// Renderable metadata for a tool call (aggregates its `start` + `finish`).
struct ToolRender {
    let callID: String
    let name: String
    /// Args captured at `start`. Optional because a `finish` may arrive
    /// without a `start` in the client's window.
    let args: [String: JSONValue]?
    /// Result captured at `finish` (nil while in progress).
    let result: JSONValue?
    /// Elapsed seconds reported at `finish` (nil while in progress).
    let duration: Double?
    /// Whether the `finish` frame has landed.
    let finished: Bool
}

/// One composed row in the live chat transcript.
///
/// Pure render data: each case carries exactly what its row draws. Identity
/// lives on the wrapping `ChatRow` (a stable `id` that survives streaming),
/// not here — so these enum cases stay clean and pattern-matchable.
enum ChatItem {
    /// A message turn. `streaming == true` while deltas are still arriving;
    /// the final delta flips it false. `role` distinguishes the operator's
    /// echo ("user") from the orchestrator ("assistant").
    case message(role: String, text: String, streaming: Bool)
    /// A tool call, composed from its start+finish frames.
    case tool(ToolRender)
    /// A kanban change (tappable chip in the view).
    case card(CardPayload)
    /// A subagent spawned / finished.
    case agent(AgentPayload)
    /// An ambient session fact (cron, crash, escalation, compaction…).
    case system(SystemPayload)
    /// A named, actionable failure.
    case error(ErrorPayload)
    /// A wire type this build does not know — surfaced raw so nothing drops.
    case unknown(type: String, raw: String)
    /// A synthetic notice row the client raises (reconnected, gap detected) —
    /// never a server event, always client-local framing.
    case notice(String)
}

/// A composed chat row: the render data (`item`) plus the stable identity the
/// streaming lifetime needs. `id` is chosen once at row creation so appends to
/// that row (message deltas, tool finish) keep the same identity and SwiftUI
/// can animate them without tearing the list.
struct ChatRow: Identifiable {
    let id: String
    var item: ChatItem

    /// A short factor for debug ("assistant"), for tests and the view footer.
    var kindLabel: String {
        switch item {
        case .message(let role, _, _): return role
        case .tool: return "tool"
        case .card: return "card"
        case .agent: return "agent"
        case .system: return "system"
        case .error: return "error"
        case .unknown: return "unknown"
        case .notice: return "notice"
        }
    }
}

// MARK: - Printable rendering for JSONValue (tool args / results / details)

extension JSONValue {
    /// Render a JSON value as one scannable line (for chip captions and the
    /// expanded panel header).
    func renderInline() -> String {
        switch self {
        case .string(let s): return s
        case .int(let i): return String(i)
        case .double(let d): return String(d)
        case .bool(let b): return String(b)
        case .null: return "null"
        case .object(let o):
            let pairs = o.keys.sorted().map { key in "\"\(key)\": \(o[key]?.renderInline() ?? "null")" }
            return "{ " + pairs.joined(separator: ", ") + " }"
        case .array(let a):
            return "[ " + a.map { $0.renderInline() }.joined(separator: ", ") + " ]"
        }
    }
}

extension Dictionary where Key == String, Value == JSONValue {
    /// Render an args (or result) dict into a label: single-line for the
    /// collapsed chip, or pretty multi-line JSON when the chip is expanded.
    func renderArgs(pretty: Bool = false) -> String {
        let separator = pretty ? "\n" : "  "
        let pairs = keys.sorted().map { key -> String in
            let v = self[key]?.renderInline() ?? "null"
            return "\(key): \(v)"
        }
        return pairs.joined(separator: separator)
    }
}
