import Foundation

/// One row in a project's persisted orchestrator-chat transcript.
///
/// Lives here because `ChatStore` persists an array of these per project
/// (`UserDefaults`, JSON) and `ProjectsView` reads them for the last-reply
/// preview. It used to be declared in `OrchestratorChatView.swift`, which was
/// retired in the IOS_REDESIGN pass (0 call sites — the live chat is
/// `StreamingChatView`); the model outlived that view, so it moved here rather
/// than being deleted with it.
///
/// Codable is hand-written (tagged payload) so a new case can be added without
/// breaking transcripts already persisted on an operator's phone.
enum ChatEntry: Codable, Equatable {
    /// A prompt the operator sent and the orchestrator received (delivered).
    case prompt(String)
    /// A completed orchestrator reply.
    case reply(String)
    /// A terminal failure — red, never rendered as a reply. Used for a job that
    /// reached a terminal error on the server, and for the honest terminal
    /// states this chat reaches when a reply cannot be collected (t_c0953d4c).
    case failure(String)
    /// An UNSENT message: the send failed to DELIVER (the POST never created a
    /// job). It stays visible, marked UNSENT, with a Retry — never silently
    /// discarded (t_c0953d4c). Holds the failed prompt text and an optional
    /// human reason for why delivery failed (transport, bad request, etc.).
    case unsent(prompt: String, reason: String?)
    /// A QUEUED message: the cluster was unreachable at send time (t_42ba90d2).
    /// The message is held by the app-scoped `OfflineSendQueue` and will be sent
    /// when the connection returns. Rendered distinctly so the operator knows it
    /// has NOT landed yet. The `messageID` links this entry to its queue item.
    case queued(text: String, messageID: UUID)

    var text: String {
        switch self {
        case .prompt(let t): return t
        case .reply(let t): return t
        case .failure(let t): return t
        case .unsent(let prompt, _): return prompt
        case .queued(let text, _): return text
        }
    }

    /// The delivery-failure reason, for the `.unsent` case (nil for all others).
    var unsentReason: String? {
        if case .unsent(_, let reason) = self { return reason }
        return nil
    }

    // MARK: Codable — encode the case + payload as a tagged payload so a
    // future case can be added without breaking old persisted transcripts.

    private enum Kind: String, Codable {
        case prompt, reply, failure, unsent, queued
    }

    private struct Payload: Codable {
        let kind: Kind
        let text: String
        /// Present only for `.unsent` — the reason delivery failed. Optional so
        /// older (pre-unsent) persisted records and reason-less unsents decode.
        var reason: String? = nil
        /// Present only for `.queued` — links the entry to its offline-queue
        /// item. Optional (and defaults nil on decode) so transcripts queued
        /// before this field shipped still decode; a nil ids simply never
        /// reconciles, which is safe.
        var messageID: UUID? = nil
    }

    init(from decoder: Decoder) throws {
        let payload = try Payload(from: decoder)
        switch payload.kind {
        case .prompt: self = .prompt(payload.text)
        case .reply: self = .reply(payload.text)
        case .failure: self = .failure(payload.text)
        case .unsent: self = .unsent(prompt: payload.text, reason: payload.reason)
        case .queued:
            // A queued entry with a missing/undecodable id cannot link to any
            // queue item. Treat it as never-delivered failure rather than a
            // silent black-box — the operator re-sends.
            self = .queued(text: payload.text, messageID: payload.messageID ?? UUID())
        }
    }

    func encode(to encoder: Encoder) throws {
        let kind: Kind
        var reason: String? = nil
        var messageID: UUID? = nil
        switch self {
        case .prompt(let t):
            kind = .prompt
            _ = t
        case .reply(let t):
            kind = .reply
            _ = t
        case .failure(let t):
            kind = .failure
            _ = t
        case .unsent(let prompt, let r):
            kind = .unsent
            _ = prompt
            reason = r
        case .queued(let t, let mid):
            kind = .queued
            _ = t
            messageID = mid
        }
        try Payload(kind: kind, text: text, reason: reason, messageID: messageID).encode(to: encoder)
    }
}
