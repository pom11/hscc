import Foundation

// ===========================================================================
// StreamReplyWatcher — the "reply finished while I was elsewhere" detector.
//
// The live chat (StreamingChatView/StreamingChatStore) is a window onto a
// project's session: it only knows about replies while its WebSocket is open.
// The moment the operator leaves the chat (or the app goes to another tab)
// the socket closes, so a reply that finishes afterwards would sit invisible
// — the exact "I have to switch tabs to see it" complaint (t_c9cc4ef9).
//
// This type closes that gap. Modeled on ApprovalPoller (the existing
// foreground poller): while a client is set it polls every project's newest
// session page and reports NEW orchestrator replies to the unread center. It
// is purely OBSERVATIONAL — it never mutates anything server-side.
//
// Watermark (why it is correct, not just present):
//   * A per-project seq baseline is the highest seq the app has already
//     ACCOUNTED FOR — either because the live chat folded it (the operator
//     SAW it) or because this watcher counted it.
//   * First observation of a project establishes the baseline WITHOUT counting:
//     older history is "prior art", not "new since you last read". Without
//     this, the first poll on a fresh install would badge every reply in every
//     project's whole history at once.
//   * Thereafter only `message(role == "assistant", done == true)` events with
//     seq > the baseline are counted. The live chat advances the same baseline
//     via `noteSeen`, so a reply the operator already read in the chat is never
//     re-badged after they navigate away.
//   * `noteReply` itself suppresses the project the operator is actively reading,
//     so a reply that lands WHILE they read that chat isn't badged either.
//
// The baseline must be shared with the live stream, so this single watermark
// lives here and the streaming store advances it.
// ===========================================================================

/// Detects orchestrator replies that finish while the operator is not reading
/// the project's chat, and reports them to `ProjectUnreadCenter` (the app's
/// only notification mechanism) so the project-list badge says "a reply is
/// waiting". Purely observational; foreground timer, like `ApprovalPoller`.
@MainActor
final class StreamReplyWatcher: ObservableObject {

    private static let pollInterval: TimeInterval = 30

    private let unread: ProjectUnreadCenter
    private var client: HSCCClient?
    private var timer: Timer?
    /// Per-project watermark: the highest seq already accounted for (seen live
    /// OR counted). Absent key = this project has never been observed.
    /// Held here so the live chat AND the poll share ONE source of truth.
    private var baseline: [String: Int] = [:]
    /// Guard so overlapping polls for the same project never double-count
    /// (`noteReply` is idempotent per reply, but we never want two in-flight
    /// fetches racing to count the same seq).
    private var inFlight: Set<String> = []

    init(unread: ProjectUnreadCenter) {
        self.unread = unread
    }

    /// The live chat reports the highest seq it has folded for `project`.
    /// Establishes the baseline on first view or advances it — so a reply the
    /// operator already SAW in the chat is never re-badged by the poll.
    func noteSeen(project: String, seq: Int) {
        if let current = baseline[project], current >= seq { return }
        baseline[project] = seq
    }

    /// Attach the current client (or nil when unconfigured). Restarts the poll.
    func setClient(_ client: HSCCClient?) {
        self.client = client
        timer?.invalidate()
        timer = nil
        guard client != nil else { return }
        Task { await refreshAll() }
        timer = Timer.scheduledTimer(withTimeInterval: Self.pollInterval,
                                   repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in await self?.refreshAll() }
        }
    }

    /// Poll every project's newest session page for NEW assistant replies.
    func refreshAll() async {
        guard let client else { return }
        let projects = (try? await client.projects().projects) ?? []
        for project in projects {
            await refresh(project: project.name, client: client)
        }
    }

    /// Fetch one project's newest session page and count replies newer than the
    /// shared watermark. On the first-ever observation, baseline WITHOUT counting
    /// (older history is prior art, never a wall of badges).
    func refresh(project: String, client: HSCCClient) async {
        guard !inFlight.contains(project) else { return }
        inFlight.insert(project)
        defer { inFlight.remove(project) }
        guard let page = try? await client.sessionEvents(project: project) else {
            // transient / offline — try again next poll; never guess.
            return
        }
        let events = page.events.sorted { $0.seq < $1.seq }
        guard let highest = events.map(\.seq).max() else { return }

        if let previous = baseline[project] {
            // Watermark already established — count only replies strictly newer.
            for event in events where event.seq > previous {
                if isAssistantReply(event) {
                    unread.noteReply(project: project)
                }
            }
            baseline[project] = max(previous, highest)
        } else {
            // First observation: anchor to the current tail WITHOUT counting.
            baseline[project] = highest
        }
    }

    /// A completed orchestrator reply: a `message` event, role == "assistant",
    /// `done == true` (a whole turn in history, or the final delta live).
    private func isAssistantReply(_ event: SessionEvent) -> Bool {
        guard case .message(let m) = event.payload, m.role == "assistant", m.done else {
            return false
        }
        return true
    }
}
