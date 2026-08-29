import Foundation
import Combine

/// Per-project unread badge state (t_267da363).
///
/// This is the app's ONLY notification mechanism — there is no push. Without
/// it, a reply the orchestrator finishes while the operator is elsewhere sits
/// silently in the project's chat until they happen to open it. The unread
/// badge on the project list row is what makes "the reply waits in the
/// app" workable rather than annoying: the operator sends a prompt, leaves the
/// chat, and the badge tells them when the answer is waiting.
///
/// Driven from SESSION EVENTS: the single funnel where an orchestrator reply
/// lands in a project's session is `ChatStore.finishSend` (whether collected
/// by a live poll or a resumed job after relaunch), so that is the ONE place
/// that reports a reply to this center (`noteReply`). Cleared on read: opening
/// a project's chat (`markRead` + declaring it the `readingProject`) zeroes
/// that project's count, and a reply that arrives while the operator IS
/// reading that chat is not counted at all. Shown on the project list:
/// `ProjectsView.projectRow` renders the badge from `count(for:)`.
///
/// Counts are keyed by project NAME and persisted in UserDefaults so a badge
/// survives app relaunch — the whole point of the mechanism is that the reply
/// can finish long after the operator navigated away or left the app.
///
/// Purely observational wrt the cluster: it never mutates anything server-side.
/// `noteReply` / `markRead` are idempotent; concurrent double-firing is safe.
@MainActor
final class ProjectUnreadCenter: ObservableObject {

    /// Per-project unread counts, keyed by project name. Absent key == 0.
    @Published private(set) var unread: [String: Int] = [:]

    /// The project whose chat is currently on screen, if any. Used to decide
    /// whether a just-arrived reply is read (visible) or unread (needs a
    /// badge). Set by the chat view on appear/disappear.
    private(set) var readingProject: String?

    // MARK: - Persistence

    // Internal (not private) so the state-machine harness can reset it between
    // runs — the center persists ALL projects in one blob, so a prior CLI run
    // leaves counts that would make the harness non-deterministic.
    static let storageKey = "hscc.unread.projects"

    init() {
        let defaults = UserDefaults.standard
        if let data = defaults.data(forKey: Self.storageKey),
           let decoded = try? JSONDecoder().decode([String: Int].self, from: data) {
            self.unread = decoded
        }
    }

    // MARK: - Session-event entry points

    /// The orchestrator finished a reply for `project`'s session — the session
    /// event that drives the badge. Counts it as unread UNLESS the operator is
    /// currently reading that project's chat (then the reply is already on
    /// screen, so a badge would be noise). Idempotent and safe to call more
    /// than once for the same reply (a replay/re-poll folds the same reply,
    /// but each distinct new reply increments).
    func noteReply(project: String) {
        guard readingProject != project else { return }
        unread[project] = (unread[project] ?? 0) + 1
        persist()
    }

    /// Clear a project's unread count — the operator has read the waiting
    /// replies. Called when the project's chat is opened. `nil`/absent counts
    /// are left alone so this never writes a spurious zero for a project that
    /// has no entry.
    func markRead(project: String) {
        guard let current = unread[project], current > 0 else { return }
        unread.removeValue(forKey: project)
        persist()
    }

    /// Declare which project's chat is currently on screen (or clear it with
    /// nil when the operator leaves the chat). Governing `noteReply`'s
    /// read/not-read decision.
    func setReading(_ project: String?) {
        readingProject = project
    }

    /// The unread count for a project — what the project-list row's badge
    /// shows. Absent key reads as 0 (no badge).
    func count(for project: String) -> Int {
        unread[project] ?? 0
    }

    private func persist() {
        if let data = try? JSONEncoder().encode(unread) {
            UserDefaults.standard.set(data, forKey: Self.storageKey)
        }
    }
}
