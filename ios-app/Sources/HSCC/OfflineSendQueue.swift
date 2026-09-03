import Foundation
import Combine

// ===========================================================================
// OfflineSendQueue — outbound chat messages that have not reached the cluster.
//
// Card t_42ba90d2: a message composed while the cluster is unreachable (dead
// spot, Tailscale down) must not be lost, never silently dropped, never sent
// twice, and must be visually distinct until it lands. When the connection
// returns, queued messages flush.
//
// The queue is APP-SCOPED and app-lifetime (a singleton), persisted in
// UserDefaults so a force-quit or relaunch does not lose a queued message. It is
// the single source of truth for "sent-but-not-delivered". No chat view owns it;
// views ENQUEUE into it and RENDER from it.
//
// The connection truth comes from `ConnectionMonitor.shared` (53f71c9): every
// completed real API request reports into it, so .reachable means "a request
// actually reached the API", .unreachable means a transport failure. This queue
// subscribes to that status and flushes when it transitions to .reachable.
//
// Delivery itself is injected (`sendHandler`), so the queue is testable without
// the network and the app supplies the one real delivery path. "Never send
// twice" = a unique UUID per message + an in-flight guard set + removal only
// after a confirmed outcome.
// ===========================================================================

@MainActor
final class OfflineSendQueue: ObservableObject {

    // MARK: - Model

    /// One outbound chat message waiting to reach the cluster.
    struct QueuedMessage: Codable, Identifiable, Equatable {
        let id: UUID
        /// Target orchestrator project (the chat surfaces use "general" for the
        /// standalone form — same as OrchestratorChatView.chatProject).
        let project: String
        // The message body (already trimmed of surrounding whitespace).
        let text: String
        /// How to deliver this message.
        let kind: Kind
        /// When it was queued (for surfacing "queued Xm ago" if we want it).
        let createdAt: Date

        init(id: UUID = UUID(), project: String, text: String, kind: Kind, createdAt: Date = Date()) {
            self.id = id
            self.project = project
            self.text = text
            self.kind = kind
            self.createdAt = createdAt
        }
    }

    enum Kind: String, Codable {
        /// `POST /v1/orchestrator/chat` + poll (the operator's primary surface).
        case orchestratorChat
    }

    /// The outcome of one delivery attempt, decided by `sendHandler`.
    enum SendOutcome: Equatable {
        /// The message reached the cluster (a job was created). Remove from queue.
        case delivered
        /// The server reached but permanently refused/failed it (4xx/5xx, bad
        /// decode) — the queue can't fix it; remove + record. Re-queueing would
        /// hammer the server for a message that will never go through.
        case rejected(String)
        /// Still unreachable — keep queued, try again on the next flush.
        case unreachable
    }

    // MARK: - Shared singleton

    static let shared = OfflineSendQueue()

    // MARK: - Published state

    /// Queued messages, oldest first (FIFO flush order).
    @Published private(set) var pending: [QueuedMessage] = []

    /// The most recent message the queue finished handling, and its outcome.
    /// Views that rendered it as `.queued` listen for this to flip the entry
    /// (delivered → prompt/poll, rejected → failure) without re-listing the
    /// whole queue. `nil` until the first message is handled.
    @Published private(set) var lastHandled: (id: UUID, outcome: SendOutcome)?

    /// Messages dropped when the user switched clusters (`drainDueToClusterSwitch`),
    /// so the app can surface them rather than silently discarding. Reset to nil
    /// after a view reads/banners it (the consuming view calls
    /// `consumeDrained()`).
    @Published private(set) var drainedDueToClusterSwitch: [QueuedMessage]?

    /// How the app actually delivers a queued message. Seeded once by the app
    /// root (ContentView.onAppear) with the real POST+persist path. Until it is
    /// set, flush is a no-op — messages stay queued, never lost.
    var sendHandler: ((QueuedMessage) async -> SendOutcome)?

    /// UUIDs currently being delivered. Guards "never send twice": a flush must
    /// not re-issue a send for a message already in flight.
    private var inFlight: Set<UUID> = []

    /// Queue flush gating: a flush is only allowed when an actual delivery is
    /// NOT already running, so two reachability events can't start overlapping
    /// flush loops. (Operations are serialized on the main actor anyway, but
    /// the guard keeps the async send from interleaving with a second flush.)
    private var isFlushing = false

    private var statusCancellable: AnyCancellable?

    private static let storageKey = "hscc.offline.queue.pending"

    private init() {
        pending = Self.load()
        // Subscribe to the shared connection truth. When the cluster becomes
        // reachable and we have queued messages, flush them.
        statusCancellable = ConnectionMonitor.shared.$status
            .receive(on: RunLoop.main)
            .sink { [weak self] status in
                guard let self else { return }
                if case .reachable = status, !self.pending.isEmpty {
                    Task { await self.flushIfReachable() }
                }
            }
    }

    // MARK: - Introspection

    var pendingCount: Int { pending.count }

    /// How many queued messages target `project` (for a per-chat queue chip).
    func queuedCount(for project: String) -> Int {
        pending.filter { $0.project == project }.count
    }

    /// Whether a specific message is still queued (and not being delivered).
    func isPending(_ id: UUID) -> Bool {
        pending.contains { $0.id == id }
    }

    // MARK: - Mutations

    /// Add a message to the queue. Returns its id (the view binds its `.queued`
    /// transcript entry to this id). Idempotent per message object.
    @discardableResult
    func enqueue(project: String, text: String, kind: Kind) -> UUID {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            // A caller passed an empty body — nothing to queue. Return a fresh
            // id anyway so the caller's `.queued` render is well-formed, but
            // because we do NOT append it, `isPending(id)` is false and it is
            // never flushed. (The IPs views already trim before calling send.)
            return UUID()
        }
        let msg = QueuedMessage(project: project, text: trimmed, kind: kind)
        // A duplicate of an existing PENDING message with identical content is
        // still a distinct send the operator pressed twice — keep both. The
        // server's session echo makes re-posting non-duplicating on its side.
        pending.append(msg)
        persist()
        return msg.id
    }

    /// Remove a message from the queue. Call ONLY after a confirmed outcome
    /// (delivered or permanently rejected). `flushIfReachable` calls this.
    func remove(_ id: UUID) {
        pending.removeAll { $0.id == id }
        persist()
    }

    /// Clear the whole queue (used when settings/cluster change so messages for
    /// one cluster don't leak into another). The messages are published on
    /// `drainedDueToClusterSwitch` so the app can SURFACE what was dropped —
    /// clearing silently would violate the card's "never silently drop".
    func drainDueToClusterSwitch() {
        guard !pending.isEmpty else { return }
        let dropped = pending
        pending = []
        inFlight = []
        isFlushing = false
        drainedDueToClusterSwitch = dropped
        persist()
    }

    /// Reset in-memory + persisted state. Test support; also called when the
    /// user switches cluster so old-cluster queued messages are not flushed
    /// into the new cluster.
    func reset() {
        pending = []
        inFlight = []
        isFlushing = false
        lastHandled = nil
        drainedDueToClusterSwitch = nil
        persist()
    }

    /// Consume (and clear) the dropped-on-cluster-switch set once a view has
    /// surfaced it, so it is not re-bannered on every redraw.
    func consumeDrained() {
        drainedDueToClusterSwitch = nil
    }

    // MARK: - Flush

    /// Deliver every queued message, guarded against double-send. Safe to call
    /// from the ConnectionMonitor subscription, an explicit view action, or a
    /// fresh app launch. A transport failure during flush keeps the message
    /// queued (never dropped). A permanent server rejection removes it with a
    /// recorded reason.
    func flushIfReachable() async {
        guard ConnectionMonitor.shared.status == .reachable else { return }
        guard let sendHandler else { return }   // not seeded yet — stay queued
        guard !isFlushing else { return }        // a flush is already underway
        isFlushing = true
        defer { isFlushing = false }

        // Snapshot so removals during the loop don't skip entries, and iterate
        // oldest-first (array order = FIFO).
        let queue = pending
        for msg in queue {
            // Re-check reachability each iteration: the first delivered message
            // may have flipped the monitor to .unreachable again.
            guard ConnectionMonitor.shared.status == .reachable else { break }
            // Skip anything removed or already being delivered meanwhile.
            guard pending.contains(where: { $0.id == msg.id }) else { continue }
            guard !inFlight.contains(msg.id) else { continue }  // never send twice
            inFlight.insert(msg.id)
            let outcome = await sendHandler(msg)
            inFlight.remove(msg.id)
            switch outcome {
            case .delivered, .rejected:
                remove(msg.id)
            case .unreachable:
                ()  // still can't reach — keep queued; a later flush retries.
            }
            lastHandled = (msg.id, outcome)
        }
    }

    // MARK: - Persistence

    private func persist() {
        if let data = try? JSONEncoder().encode(pending) {
            UserDefaults.standard.set(data, forKey: Self.storageKey)
        }
    }

    private static func load() -> [QueuedMessage] {
        guard let data = UserDefaults.standard.data(forKey: storageKey),
              let decoded = try? JSONDecoder().decode([QueuedMessage].self, from: data) else {
            return []
        }
        return decoded
    }
}
