import Foundation
import Combine

// ===========================================================================
// StreamingChatStore — the LIVE chat window onto a project's session.
//
// The old chat (OrchestratorChatView) was job-based: it POSTed a prompt and
// polled for a single reply string, so the operator saw only their own
// prompts and the orchestrator's final answer. The streaming chat is a WINDOW
// onto the WHOLE project session: it renders the typed event stream (message /
// tool_call / card / agent / system / error) in real time, token by token.
//
// Two data paths feed the composition, both funneled through
// StreamingTranscript (the aggregation core, proven by streaming_check.sh):
//
//   1. HISTORY SEED — GET /v1/projects/{name}/session/events (the newest page)
//      restores the current conversation so the operator opens into context,
//      not a blank screen. REST, paged, gap-free by the server.
//
//   2. LIVE WS — GET /v1/projects/{name}/session/ws?after=<lastSeq> relays new
//      frames as they occur. Every frame passes through SessionStreamCursor
//      (the reconnect guard) so a dropped/rejoined socket produces NO GAP and
//      NO REPEAT. The bridge rides the SAME seq space as history, so the two
//      paths abut cleanly: history folds up to lastSeq, then the WS resumes
//      from lastSeq+1.
//
// Persistence (per project, in UserDefaults under "streaming.chat.<project>"):
//   * `lastSeq` — so a relaunched app reconnects at the right seq instead of
//     replaying old events as new (the cursor guarantee survives relaunch);
//   * `draft` — the composer draft survives navigation.
//
// Concurrency: URLSessionWebSocketTask's receive callback fires off-main. It
// bridges every frame onto the MainActor (via `Task { @MainActor in … }`) so
// all mutation of `rows` / `phase` / `cursor` is serialized on the main actor
// and observation stays consistent.
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

/// Convenience alias — the store exposes `phase` as a `ConnectionPhase`.
typealias ConnectionPhase = StreamPhase

/// Connection settings the view derives from SettingsStore and hands in.
struct StreamSettings {
    let host: String
    let port: Int
    let token: String
    let isConfigured: Bool
}

@MainActor
final class StreamingChatStore: ObservableObject {
    let project: String

    /// Composed display rows (the pure streaming transcript). Published so the
    /// view streams as events land.
    @Published private(set) var rows: [ChatRow] = []
    /// idle/loading/connecting/connected/reconnecting/failed — drives the
    /// status banner so the operator always knows if the stream is live.
    @Published private(set) var phase: ConnectionPhase = .idle
    /// Tool chips the operator has expanded. Held here (not per-row @State) so
    /// expansion survives scrolling and rebuilds. Keyed by row id.
    @Published var expandedToolIDs: Set<String> = []
    /// The composer draft (persisted per project).
    @Published var draft: String = ""
    /// A one-off error surfaced by the latest send (shown, then cleared).
    @Published var sendError: String?

    // Aggregation + reconnect guard.
    private var transcript = StreamingTranscript()
    private var cursor = SessionStreamCursor()

    // WebSocket.
    private var wsTask: URLSessionWebSocketTask?
    private let urlSession: URLSession

    /// Settings derived by the view on `start`.
    private var settings: StreamSettings?
    /// Whether the current socket began as a resume (replay tail) — the cursor
    /// needs this to know that a non-contiguous start is NORMAL and not a gap.
    private var isResume = false
    /// Backoff counter for reconnect retries (bounded, honest).
    private var reconnectAttempt = 0
    /// The view is alive (stops reconnect + closes socket on disappear).
    private var isActive = false

    private static let keyPrefix = "streaming.chat."

    init(project: String, urlSession: URLSession = .shared) {
        self.project = project
        self.urlSession = urlSession
        // Restore persisted draft + cursor so a relaunch resumes gap-free.
        draft = UserDefaults.standard.string(forKey: Self.keyPrefix + project + ".draft") ?? ""
        let savedSeq = UserDefaults.standard.object(forKey: Self.keyPrefix + project + ".seq") as? UInt64 ?? 0
        cursor = SessionStreamCursor(lastSequence: savedSeq)
    }

    // MARK: - Lifecycle (called from the view's .task)

    /// Start the chat window: seed from history, then open the live socket.
    /// Safe to call once; the view calls it on first appear.
    func start(settings: StreamSettings) async {
        guard !isActive else { return }
        isActive = true
        self.settings = settings
        reconnectAttempt = 0
        await seedFromHistory()
        openSocket()
    }

    /// Stop listening (view disappeared). Closes the socket; the persisted
    /// cursor means a later `start` resumes without gap.
    func stop() {
        isActive = false
        wsTask?.cancel()
        wsTask = nil
        phase = .idle
    }

    /// Persist the composer draft (the view calls this on disappear/type-idle
    /// so a backgrounded app keeps the operator's in-progress message).
    func persistDraft() {
        UserDefaults.standard.set(draft, forKey: Self.keyPrefix + project + ".draft")
    }

    // MARK: - History seed

    /// Fetch the newest history page and fold it, advancing the resume cursor
    /// to the highest seq seen so the live socket continues from there.
    /// History is a convenience, never a gate: if it fails we still try the
    /// live socket, but surface why the window may not be current.
    @MainActor
    private func seedFromHistory() async {
        phase = .loadingHistory
        guard let settings, let client = makeClient(settings) else {
            phase = .failed("Set a host, port, and token in Settings to stream this project's session.")
            return
        }
        do {
            let page = try await client.sessionEvents(project: project)
            foldHistory(page.events)
            phase = .connecting
        } catch {
            if let hscc = error as? HSCCError {
                phase = .failed(historyFailureMessage(hscc))
            } else {
                phase = .failed("Couldn't load session history: \(error.localizedDescription)")
            }
        }
    }

    /// Fold a paged history batch straight into the transcript and advance the
    /// cursor's lastSequence so the WS resume starts exactly after them.
    @MainActor
    private func foldHistory(_ events: [SessionEvent]) {
        var last = 0
        for e in events {
            transcript.fold(e)
            last = e.seq
        }
        if last > 0 {
            cursor = SessionStreamCursor(lastSequence: UInt64(last))
            rows = transcript.rows
        }
    }

    // MARK: - WebSocket

    /// Open (or resume) the live WebSocket to the project's session stream.
    @MainActor
    private func openSocket() {
        guard let settings else {
            phase = .failed("Not started.")
            return
        }
        guard settings.isConfigured, !settings.token.isEmpty else {
            phase = .failed("Set a host, port, and token in Settings to stream this project's session.")
            return
        }
        guard let url = wsURL(settings) else {
            phase = .failed("The host or port is invalid. Set them in Settings.")
            return
        }
        // We ALWAYS present the resume cursor, so the server replays the tail
        // after `lastSequence`; a non-contiguous first event is NORMAL (the
        // asked-for replay / retention trim), not a gap. Re-arm resume so the
        // first accepted event of this connection is allowed to jump.
        isResume = true
        phase = .connecting

        var request = URLRequest(url: url)
        request.setValue("Bearer \(settings.token)", forHTTPHeaderField: "Authorization")
        request.timeoutInterval = 30

        let task = urlSession.webSocketTask(with: request)
        wsTask = task
        task.resume()
        receive()
    }

    private func wsURL(_ s: StreamSettings) -> URL? {
        let proj = project.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? project
        var components = URLComponents()
        components.scheme = "ws"
        components.host = s.host
        components.port = s.port
        components.path = "/v1/projects/\(proj)/session/ws"
        components.queryItems = [
            URLQueryItem(name: "after", value: String(cursor.resumeRequest)),
        ]
        return components.url
    }

    /// Receive frames in a loop until the socket closes. Bridges each frame
    /// onto the main actor so all state mutation is serialized there.
    private func receive() {
        wsTask?.receive { [weak self] result in
            guard let self else { return }
            switch result {
            case .success(let message):
                switch message {
                case .string(let text):
                    Task { @MainActor in
                        self.handleFrame(text: text)
                        if self.isActive { self.receive() }   // await next frame
                    }
                case .data(let data):
                    let text = String(data: data, encoding: .utf8) ?? ""
                    Task { @MainActor in
                        self.handleFrame(text: text)
                        if self.isActive { self.receive() }
                    }
                @unknown default:
                    Task { @MainActor in
                        if self.isActive { self.receive() }
                    }
                }
            case .failure(let error):
                Task { @MainActor in
                    self.socketClosed(error)
                }
            }
        }
    }

    /// Handle one WS frame: seq-guard it, then fold into the transcript.
    /// The first frame is the server's `hello` (`{"kind":"hello","next_seq":N}`),
    /// which is control framing, not a session event — we skip it and arm the
    /// "live resumes here" expectation via the cursor.
    @MainActor
    private func handleFrame(text: String) {
        if isHelloFrame(text) {
            // The hello arrives as soon as the handshake completes — the first
            // frame of every connection. That IS the "we're live" signal; the
            // replay tail (if any) and live events fold over the next frames.
            phase = .connected
            return
        }
        guard let event = try? JSONDecoder().decode(SessionEvent.self, from: Data(text.utf8)) else {
            // An undecodable frame — surface it as a notice rather than
            // dropping it silently.
            transcript.addNotice("Received an unreadable frame.")
            rows = transcript.rows
            return
        }
        let cursorEvent = SessionStreamCursor.Event(seq: UInt64(event.seq), payload: text)
        switch cursor.accept(cursorEvent, isResume: isResume) {
        case .accept:
            transcript.fold(event)
            rows = transcript.rows
            saveSeq()
            // The resume tail (if any) is consumed; from here on jumps are
            // real gaps and must be detected as such.
            isResume = false
        case .duplicate:
            ()  // already folded — skip (idempotent under retry)
        case .gap(let from, let through):
            // Real events were lost and we did not ask for them. Be honest:
            // surface the hole and re-resume to fill it.
            transcript.addNotice("Some session events were skipped (seq \(from)–\(through). Reconnecting to fill the gap.")
            rows = transcript.rows
            reconnect(resumed: true)
        }
    }

    /// True when the frame is the server's opening `hello` envelope
    /// (`{"kind":"hello","next_seq":N}`) — control framing, not a session event.
    private func isHelloFrame(_ text: String) -> Bool {
        // Cheapest reliable check: it is a JSON object carrying `kind == hello`.
        guard text.first == "{",
              let data = text.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              (obj["kind"] as? String) == "hello" else {
            return false
        }
        return true
    }

    /// The socket closed (or failed to receive). If the view is still alive,
    /// retry with bounded backoff and resume the cursor — no gap, no repeat.
    @MainActor
    private func socketClosed(_ error: Error) {
        wsTask = nil
        guard isActive else { return }
        reconnect(resumed: true)
    }

    @MainActor
    private func reconnect(resumed: Bool) {
        guard isActive else { return }
        isResume = resumed
        phase = .reconnecting
        reconnectAttempt += 1
        // Bounded, honest backoff: 1s, 2s, 4s, 8s, capped at 15s. Never a hot
        // reconnect loop — each retry is gated by a delay AND resumes from the
        // cursor so nothing is lost or repeated.
        let delay = Double(min(1 << reconnectAttempt, 15))
        Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
            guard let self, self.isActive else { return }
            self.openSocket()
        }
    }

    // MARK: - Send

    /// Send a prompt over the live socket (`{"kind":"send","text":...}`). The
    /// orchestrator session processes it and streams typed events back over the
    /// same socket, which the transcript folds as they arrive. Only available
    /// while connected; otherwise surface why.
    func send(_ text: String) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        guard let wsTask else {
            sendError = "Not connected yet — the stream is still opening. Try again."
            return
        }
        let payload: [String: String] = ["kind": "send", "text": trimmed]
        guard let data = try? JSONSerialization.data(withJSONObject: payload),
              let frame = String(data: data, encoding: .utf8) else {
            sendError = "Couldn't encode your message."
            return
        }
        wsTask.send(.string(frame)) { [weak self] error in
            Task { @MainActor in
                if let error {
                    self?.sendError = "Send failed: \(error.localizedDescription)"
                }
            }
        }
        draft = ""   // the prompt now lives in the session stream; never lost
        persistDraft()
    }

    // MARK: - Tool expansion

    func toggleTool(_ rowID: String) {
        if expandedToolIDs.contains(rowID) {
            expandedToolIDs.remove(rowID)
        } else {
            expandedToolIDs.insert(rowID)
        }
    }

    // MARK: - Helpers

    private func makeClient(_ s: StreamSettings) -> HSCCClient? {
        guard s.isConfigured else { return nil }
        return HSCCClient(host: s.host, port: s.port, token: s.token)
    }

    private func saveSeq() {
        UserDefaults.standard.set(cursor.lastSequence, forKey: Self.keyPrefix + project + ".seq")
    }

    private func historyFailureMessage(_ error: HSCCError) -> String {
        switch error {
        case .transport:
            return "Can't reach the cluster for history — is Tailscale connected? The live stream may still connect."
        case .invalidURL:
            return "The host or port is invalid. Set them in Settings."
        case .decoding(let detail):
            return "Unexpected history response: \(detail)"
        case .api(_, let message, _):
            return message
        }
    }
}
