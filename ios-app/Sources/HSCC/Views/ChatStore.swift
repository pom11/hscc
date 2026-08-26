import Foundation

/// Per-project orchestrator chat persistence + in-flight state.
///
/// The orchestrator sessions PERSIST per project on the server (verified: a
/// single project is a continuing conversation, not one-shots). The phone
/// mirror must persist too, so a transcript survives navigating away and app
/// relaunch. Requirement 4 of the project-detail card: "Transcript persists
/// across navigating away and app relaunch, per project."
///
/// Each project gets its own saved transcript, keyed by project name in
/// `UserDefaults` (a transcript is a handful of short strings — trivial size).
/// The store is the single source of truth for the visible transcript and the
/// in-flight state (start time + which project/profile is answering), so the
/// view can render the honest waiting state from it.
///
/// Everything is `@MainActor` so state mutations funnel onto the main thread,
/// matching the rest of the SwiftUI app.
@MainActor
final class ChatStore: ObservableObject {

    /// The project this store is bound to ("general" for the standalone form).
    let project: String

    /// The full transcript, ordered oldest→newest.
    @Published private(set) var transcript: [ChatEntry] = []

    /// When a reply is in flight: when it started (for the elapsed ticker) and
    /// the orchestration identity answering (project + profile, from the API's
    /// /v1/projects registry & the /v1/orchestrator/chat 200 shape).
    @Published private(set) var inFlight: InFlight?

    struct InFlight: Equatable {
        let startedAt: Date
        /// The profile answering (e.g. "hscc-orch"), if the last reply told us.
        var profile: String?
    }

    init(project: String) {
        self.project = project
        self.transcript = Self.load(project: project)
    }

    /// Whether a reply is currently being awaited. Send is disabled while true
    /// — the session is sequential, one turn at a time (requirement 6).
    var isSending: Bool { inFlight != nil }

    // MARK: - Mutations

    /// Optimistically append the user's prompt and start the in-flight state.
    /// Called BEFORE the network request so the message is never lost, even if
    /// the request fails (requirement 1 + 3).
    func beginSend(prompt: String) {
        transcript.append(.prompt(prompt))
        inFlight = InFlight(startedAt: Date())
        persist()
    }

    /// Record the orchestrator's reply and end the in-flight state.
    func finishSend(reply: String, profile: String?) {
        transcript.append(.reply(reply))
        inFlight = nil
        persist()
    }

    /// Record a failure and end the in-flight state, keeping the sent prompt in
    /// the transcript so the user can see what produced the failure and re-send
    /// the same prompt if they want (requirement 3: never lose input).
    func failSend(message: String) {
        transcript.append(.failure(message))
        inFlight = nil
        persist()
    }

    /// The draft the user has typed but not yet sent. Persisted so a backgrounded
    /// or force-quit app does not lose what the operator was composing.
    @Published var draft: String = "" {
        didSet {
            if draft != oldValue {
                UserDefaults.standard.set(draft, forKey: Self.draftKey(project))
            }
        }
    }

    // MARK: - Persistence

    private static let prefix = "hscc.chat."

    private static func transcriptKey(_ project: String) -> String {
        "\(prefix)\(project).transcript"
    }

    private static func draftKey(_ project: String) -> String {
        "\(prefix)\(project).draft"
    }

    private func persist() {
        let data = try? JSONEncoder().encode(transcript)
        UserDefaults.standard.set(data, forKey: Self.transcriptKey(project))
    }

    private static func load(project: String) -> [ChatEntry] {
        guard let data = UserDefaults.standard.data(forKey: transcriptKey(project)),
              let loaded = try? JSONDecoder().decode([ChatEntry].self, from: data) else {
            return []
        }
        return loaded
    }

    /// Restore the saved draft for this project (called by the view once at init).
    func restoreDraft() {
        draft = UserDefaults.standard.string(forKey: Self.draftKey(project)) ?? ""
    }
}
