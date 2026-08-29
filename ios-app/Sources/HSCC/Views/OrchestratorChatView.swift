import SwiftUI

/// C5 — the per-project orchestrator chat view.
///
/// Sends a prompt to a project's orchestrator via `POST /v1/orchestrator/chat`
/// and shows a persistent prompt→reply transcript for that project.
///
/// This is the operator's PRIMARY surface — talking to a project's
/// orchestrator from the phone. It is designed around the MEASURED reality of
/// the live cluster:
///   * a real answer takes **16.8s floor / 30-90s typical**, so the wait is
///     designed for, not pretended away;
///   * **sessions persist per project** on the server — each project is a
///     continuing conversation, not one-shots. Nothing here resets it.
///   * the client sets a **300s** timeout for the call (HSCCClient, `timeout:`
///     param). It is never lowered — the 60s URLSession default would abort a
///     reply the server was still successfully working on.
///
/// Smoothness requirements (from the project-detail card) and how each is met:
///   1. **Optimistic send** — the user's message appears instantly via
///      `ChatStore.beginSend` before any network call.
///   2. **Honest waiting state** — an in-flight footer ticks elapsed SECONDS
///      (TimelineView, one frame/sec) and names which project/profile is
///      answering, so \"still working\" reads differently from \"hung\".
///   3. **Never lose input** — the prompt is appended to the persisted
///      transcript before sending and only cleared on success; a failure keeps
///      it and says what happened. The un-sent draft is also persisted.
///   4. **Transcript persists** per project across navigation and app relaunch
///      (`ChatStore`, UserDefaults keyed per project).
///   5. **Fleet-down case** — `/v1/autodown/status` is checked on appear and
///      before send; if the fleet is down/waking a clear banner says so rather
///      than silently hanging.
///   6. **One turn at a time** — send is disabled while a reply is in flight
///      (the session is sequential).
///
/// SENDING IS A MUTATION: the orchestrator can decompose a prompt and dispatch
/// real work onto its board. So sending follows the SAME explicit confirm
/// pattern as every other mutating surface (`MutationButton` /
/// `.confirmationDialog`): a tap on Send only ARMS the confirmation naming
/// exactly what will happen, and the request fires only after the user
/// confirms. There is no send-on-return and no other path that bypasses the
/// confirm step — this is the single place a chat request can fire.
///
/// Honest results: a non-2xx makes the client throw, appended to the
/// transcript as a FAILURE with the API's message — never as a reply.
struct OrchestratorChatView: View {
    /// When set, the chat is fixed to THIS project's orchestrator and the
    /// project picker is hidden (used from a project's detail screen). When
    /// nil (the standalone Chat surface), the picker shows and defaults to
    /// `general`.
    var project: String? = nil

    /// The effective project the chat targets: the fixed one, or the picker's.
    private var chatProject: String { project ?? "general" }

    var body: some View {
        // `.id(chatProject)` makes SwiftUI recreate the inner state whenever
        // the target project changes, so each project gets its own persisted
        // transcript AND its own store. For the fixed per-project case this is
        // constant and never fires.
        ChatBody(project: chatProject)
            .id(chatProject)
            .navigationTitle("Orchestrator")
            .navigationBarTitleDisplayMode(.inline)
    }
}

/// The per-project chat surface. Owns a `ChatStore` bound to `project` and
/// renders the persistent transcript, the honest waiting state, and the
/// confirm-gated composer.
private struct ChatBody: View {
    @EnvironmentObject private var settings: SettingsStore
    @EnvironmentObject private var unread: ProjectUnreadCenter
    let project: String

    /// Sustained poll failures (at a 2s interval) after which the cluster is
    /// judged unreachable and the honest terminal state replaces the spinner
    /// (~30s of no contact). A SINGLE failure never kills a job — the persisted
    /// job_id keeps it resumable. (t_c0953d4c)
    private static let maxPollFailures = 15
    /// Seconds an in-flight reply must pass before the operator gets a "Stop
    /// waiting" control, so an accidental tap can't end a real wait too early.
    /// (t_c0953d4c)
    private static let stopWaitingGraceSec = 20

    @StateObject private var store: ChatStore

    /// Which project/profile is answering (profile filled from the last reply).
    @State private var answeringProfile: String? = nil
    /// Fleet readiness: nil (unknown), or a state string from /v1/autodown/status.
    @State private var fleetState: String? = nil
    @State private var showConfirm = false
    /// The text of an UNSENT message awaiting a retry decision (t_c0953d4c).
    /// Non-nil only while a Retry confirmation is pending; reset after send/retry.
    @State private var retryCandidate: String? = nil
    /// Handle to the running poll task, so "Stop waiting" can cancel it and the
    /// honest impossible-to-collect states can end the spinner (t_c0953d4c).
    @State private var pollTask: Task<Void, Never>? = nil

    init(project: String) {
        self.project = project
        _store = StateObject(wrappedValue: ChatStore(project: project))
    }

    var body: some View {
        VStack(spacing: 0) {
            fleetBanner

            // The persistent prompt→reply transcript.
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(spacing: 12) {
                        if store.transcript.isEmpty {
                            emptyState
                        } else {
                            ForEach(store.transcript.indices, id: \.self) { index in
                                let entry = store.transcript[index]
                                ChatBubble(entry: entry, retry: retry(for: entry))
                                    .id(index)   // stable — transcript is append-only
                            }
                        }
                    }
                    .padding(.horizontal)
                    .padding(.vertical, 8)
                }
                .onChange(of: store.transcript.count) {
                    if !store.transcript.isEmpty {
                        withAnimation {
                            proxy.scrollTo(store.transcript.count - 1, anchor: .bottom)
                        }
                    }
                }
            }

            inFlightFooter
                .padding(.horizontal)
                .padding(.bottom, 4)

            Divider()

            composer
                .padding(.horizontal)
                .padding(.vertical, 8)
        }
        .onAppear {
            // Wire the unread-badge center (the environmentObject isn't
            // available at `init`, so attach it here — before any reply is
            // folded by the .task below). Declaring this project as reading and
            // clearing its badge runs BEFORE a resumed-job reply is counted, so
            // a reply that's already on screen is never badged as unread
            // (t_267da363).
            store.unread = unread
            unread.setReading(project)
            unread.markRead(project: project)
        }
        .onDisappear {
            // Leaving the chat: stop declaring we're reading it, so a reply
            // that arrives after navigation away is badged as unread.
            unread.setReading(nil)
        }
        .task {
            // Restore the saved draft + probe fleet readiness on first appear
            // so the operator sees the transcript, their draft, and a plain
            // fleet note rather than a silent hang (requirement 5).
            store.restoreDraft()
            await refreshFleetState()
            // Resume polling for an in-flight job persisted from a previous
            // session (t_bc242def): a backgrounded/relaunched app picks the
            // finished answer up instead of losing it.
            await resumeInFlightJob()
        }
    }

    // MARK: - Empty / fleet states

    private var emptyState: some View {
        VStack(spacing: 8) {
            Image(systemName: "bubble.left.and.bubble.right")
                .font(.largeTitle)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
            Text("Ask the \(project) orchestrator")
                .font(.headline)
            Text("Send a prompt to the \(project) orchestrator. It may decompose your request and dispatch real work onto the \(project) board. This conversation continues across sessions — the orchestrator remembers context.")
                .font(.footnote)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                .multilineTextAlignment(.center)
        }
        .padding(.top, 48)
        .padding(.horizontal, 24)
    }

    /// A plain readiness banner when the fleet is down or waking — requirement
    /// 5: tell the operator plainly rather than silently hanging.
    @ViewBuilder
    private var fleetBanner: some View {
        if let fleetState {
            let (text, color) = fleetBannerContent(fleetState)
            HStack(spacing: 8) {
                Image(systemName: "zzz")
                Text(text)
                    .font(.footnote)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .padding(.horizontal)
            .padding(.vertical, 8)
            .background(color.opacity(0.12))
        }
    }

    private func fleetBannerContent(_ state: String) -> (String, Color) {
        switch state {
        case "down":
            return ("The fleet is down — a reply may take a while, or fail. Consider waking it first.", Theme.Semantic.bad)
        case "waking":
            return ("The fleet is waking — this will take a few minutes. Your message is queued once it's up.", Theme.Semantic.warn)
        default:
            return ("", Theme.Semantic.neutral)   // not shown (state is up/idle)
        }
    }

    // MARK: - In-flight footer (honest waiting state)

    /// A live footer that ticks elapsed seconds while a reply is in flight.
    /// `TimelineView(.periodic)` re-renders once per second, so the elapsed
    /// count advances visibly — \"still working\" reads differently from a
    /// frozen spinner (requirement 2).
    @ViewBuilder
    private var inFlightFooter: some View {
        if let inFlight = store.inFlight {
            TimelineView(.periodic(from: inFlight.startedAt, by: 1)) { context in
                let elapsed = Int(context.date.timeIntervalSince(inFlight.startedAt))
                HStack(spacing: 8) {
                    Label {
                        Text(footerText(elapsed: elapsed, profile: answeringProfile ?? inFlight.profile))
                            .frame(maxWidth: .infinity, alignment: .leading)
                    } icon: {
                        // jobID == nil → still POSTing (no job yet); otherwise a
                        // job exists and we're polling. Both must reach an honest
                        // terminal state eventually — never a permanent spinner.
                        Image(systemName: inFlight.jobID != nil ? "circle.dotted" : "arrow.up.circle")
                    }
                    // Honest stop: once a job exists (so nothing in-flight is
                    // lost server-side) and the wait has passed a short grace so
                    // this isn't an accidental tap, let the operator end the
                    // spinner themselves. The terminal state explains what
                    // happened and that a late answer can still be resumed.
                    if inFlight.jobID != nil && elapsed >= Self.stopWaitingGraceSec {
                        Button("Stop waiting") {
                            stopWaiting()
                        }
                        .font(.caption)
                        .buttonStyle(.borderless)
                    }
                }
            }
        }
    }

    private func footerText(elapsed: Int, profile: String?) -> String {
        // Say which project/profile is answering so the operator can tell this
        // is real work, not a hang. Two pieces of machine truth on screen.
        let who: String
        if let profile {
            who = "\(project) / \(profile)"
        } else {
            who = project
        }
        return "\(elapsed)s — \(who) is answering. An orchestrator can take 30-90 s."
    }

    // MARK: - Composer (draft + confirm-gated send)

    private var composer: some View {
        VStack(spacing: 8) {
            HStack(alignment: .bottom, spacing: 8) {
                TextField("Ask the \(project) orchestrator…", text: $store.draft, axis: .vertical)
                    .lineLimit(1...4)
                    .textFieldStyle(.roundedBorder)
                    .disabled(store.isSending)

                // STEP 1 — a tap only arms the confirmation. No request is sent,
                // so a double-tap on Send can never double-send. A fresh typed
                // message is never a retry, so clear any pending retry target.
                Button {
                    retryCandidate = nil
                    showConfirm = true
                } label: {
                    if store.isSending {
                        ProgressView()
                            .frame(width: 24, height: 24)
                    } else {
                        Image(systemName: "paperplane.fill")
                            .frame(width: 24, height: 24)
                    }
                }
                .buttonStyle(.borderedProminent)
                .disabled(!canSend)
            }
        }
        .confirmationDialog(confirmTitle, isPresented: $showConfirm, titleVisibility: .visible) {
            // STEP 2 — the deliberate second step naming exactly what will happen.
            Button("Send") {
                if let text = retryCandidate {
                    Task { await retrySend(text) }
                } else {
                    Task { await submitSend() }
                }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text(confirmMessage)
        }
    }

    private var canSend: Bool {
        !store.isSending && !trimmed(store.draft).isEmpty
    }

    private var confirmTitle: String {
        if retryCandidate != nil {
            return "Retry this UNSENT message?"
        }
        return "Send to the \(project) orchestrator?"
    }

    private var confirmMessage: String {
        if retryCandidate != nil {
            return "Re-sends the failed prompt to the \(project) orchestrator. It may decompose your prompt and dispatch real work onto the \(project) project's board."
        }
        return "It may decompose your prompt and dispatch real work onto the \(project) project's board."
    }

    // MARK: - Fleet readiness probe

    /// Check /v1/autodown/status so the operator is told plainly when the fleet
    /// is down or waking (requirement 5) instead of silently waiting on a reply
    /// that won't come. Informational only — never a gate.
    private func refreshFleetState() async {
        guard settings.isConfigured,
              let token = settings.token,
              let port = Int(settings.port) else { return }
        let client = HSCCClient(host: settings.host, port: port, token: token)
        do {
            let status = try await client.autodownStatus()
            fleetState = status.state
        } catch {
            // Can't reach the cluster at all — the banner stays hidden and the
            // send will surface a transport error, which is honest.
            fleetState = nil
        }
    }

    // MARK: - Send / retry (confirm-gated) + async job poll

    /// Compose a NEW message from the composer draft and send it (t_c0953d4c).
    @MainActor
    private func submitSend() async {
        let text = trimmed(store.draft)
        retryCandidate = nil
        store.beginSend(prompt: text)
        store.draft = ""   // the prompt now lives in the transcript; never lost
        await deliver(text: text)
    }

    /// Re-send an UNSENT message (t_c0953d4c). Same confirm-gated path as a
    /// fresh send — the command is a mutation and never bypasses the gate. The
    /// historical UNSENT entry stays as the record of the failed attempt.
    @MainActor
    private func retrySend(_ text: String) async {
        retryCandidate = nil
        store.retry(prompt: text)   // appends a fresh `.prompt`, starts in-flight
        await deliver(text: text)
    }

    /// The shared delivery path for a fresh send and a retry: optimistically
    /// begin (already done by the caller), POST to create the job, then poll.
    /// If delivery fails (the POST never created a job), the message is kept as
    /// UNSENT — never silently discarded.
    @MainActor
    private func deliver(text: String) async {
        // Re-probe the fleet right before sending so the operator gets the most
        // current down/waking warning.
        await refreshFleetState()

        do {
            let client = try clientOrThrow()
            // POST /v1/orchestrator/chat returns 202 with a job_id immediately
            // (the orchestrator is messaged in a background thread on the
            // server) — no dead wait. Attach the id so a relaunched app can
            // resume the poll, then poll for the reply.
            let started = try await client.orchestratorChatStart(project: project, prompt: text)
            store.startPolling(jobID: started.jobID)
            pollTask = Task { await poll(jobID: started.jobID, client: client) }
        } catch {
            // Delivery failed: the POST never created a job (transport error, or
            // a 4xx/5xx before any background work started). Nothing will arrive.
            // Keep the message visible as UNSENT with a Retry — never silently
            // discarded, never rendered as a bogus reply — and surface why
            // delivery failed so the operator knows what to fix before Retry.
            store.markUnsent(reason: message(for: error))
        }
    }

    /// Poll GET /v1/orchestrator/chat/{id} until it reaches a terminal state,
    /// then fold the outcome into the store. `store.inFlight` drives the honest
    /// elapsed ticker, so "still working" reads differently from "hung".
    ///
    /// Honest terminal states (t_c0953d4c) — this loop NEVER spins forever:
    ///   * a job reaching a terminal error folds `.failure` and returns;
    ///   * a sustained run of unreachable polls (the phone cannot reach the
    ///     cluster) ends with `store.reachabilityLost()`, an honest "couldn't
    ///     collect the answer" note rather than an endless ticker;
    ///   * the operator choosing "Stop waiting" cancels this task, which ends
    ///     the spinner with `store.abandonWaiting()`.
    @MainActor
    private func poll(jobID: String, client: HSCCClient) async {
        var consecutiveFailures = 0
        while !Task.isCancelled {
            do {
                let status = try await client.orchestratorChatPoll(jobID: jobID)
                consecutiveFailures = 0
                if status.isTerminal {
                    if status.status == "done", let reply = status.reply {
                        store.finishSend(reply: reply, profile: status.profile)
                        answeringProfile = status.profile   // remember who answered, for next wait
                    } else {
                        store.failSend(
                            message: status.error.map { jobError(for: $0) }
                                ?? "The orchestrator call failed."
                        )
                    }
                    return
                }
            } catch {
                // A failed poll attempt (e.g. the phone was briefly offline).
                // Count it and keep going — a SINGLE failure must not kill the
                // job (the server keeps working and the persisted job_id
                // survives). Only a sustained run of failures terminates: after
                // `maxPollFailures` the cluster is judged unreachable, and the
                // honest terminal state replaces the endless spinner.
                consecutiveFailures += 1
                if consecutiveFailures >= Self.maxPollFailures {
                    store.reachabilityLost()
                    return
                }
            }
            try? await Task.sleep(nanoseconds: 2_000_000_000)  // poll every 2s
        }
    }

    /// The operator tapped "Stop waiting" in the in-flight footer: cancel the
    /// poll and end the spinner with an honest terminal state (t_c0953d4c).
    @MainActor
    private func stopWaiting() {
        store.abandonWaiting()   // guarded — clears inFlight + appends honest note
        pollTask?.cancel()
        pollTask = nil
    }

    /// The Retry action for an UNSENT entry: shows the confirm-gated retry
    /// dialog targeting the exact failed text. Only `.unsent` entries get one
    /// (there is nothing to retry otherwise). Each failed message stays
    /// individually retriable — the failed prompt is never silently dropped.
    /// The command (re-send) is a mutation and stays behind the confirm gate.
    private func retry(for entry: ChatEntry) -> (() -> Void)? {
        switch entry {
        case .unsent:
            return { [store] in
                retryCandidate = entry.text
                showConfirm = true
            }
        default:
            return nil
        }
    }

    /// Resume polling for an in-flight job persisted from a previous session, so
    /// a backgrounded/relaunched app picks the (possibly already-computed) reply
    /// up instead of losing it (t_bc242def). Called on appear.
    @MainActor
    private func resumeInFlightJob() async {
        guard store.inFlight == nil,
              let savedJobID = store.resumedJobID,
              !savedJobID.isEmpty else { return }
        do {
            let client = try clientOrThrow()
            await poll(jobID: savedJobID, client: client)
        } catch {
            // Client construction failed (settings changed); the job remains
            // persisted and the view re-attempts on the next appear.
        }
    }

    /// Map a failed job's unified `ChatJobError` to a clear human string.
    private func jobError(for error: ChatJobError) -> String {
        switch error.code {
        case "orchestrator_unavailable":
            // A real state: the orchestrator's NAMED session must exist
            // (created by provisioning / the first Telegram topic) before it can
            // be chatted with.
            return "\(error.message) The \(project) orchestrator's session isn't ready yet — create it first, then re-send."
        case "orchestrator_timeout":
            // No hardcoded seconds here: the server's timeout is configurable
            // (chat_timeout, default 600s) and the job's `elapsed`/`message`
            // carry the real number — hardcoding "180 s" would contradict them.
            return "The \(project) orchestrator did not reply in time (timeout). Try again or check the orchestrator."
        case "orchestrator_error":
            return "The \(project) orchestrator call failed: \(error.message)"
        default:
            return error.message
        }
    }

    /// Build a clear, human-facing failure string from the thrown error.
    /// Distinguishes the "session not ready" and "timeout" states from a
    /// generic failure so each real condition reads clearly.
    private func message(for error: Error) -> String {
        if let hscc = error as? HSCCError {
            switch hscc {
            case .api(let code, let message, let status):
                switch code {
                case "orchestrator_unavailable":
                    // A real state: the orchestrator's NAMED session must exist
                    // (created by provisioning / the first Telegram topic) before
                    // the orchestrator can be chatted with.
                    return "\(message) The \(project) orchestrator's session isn't ready yet — create it first, then re-send."
                case "orchestrator_timeout":
                    return "The \(project) orchestrator did not reply in time (timeout). Try again or check the orchestrator."
                default:
                    // 400 unknown_project / bad_request, 409, 502 orchestrator_error, etc.
                    if status == 502 {
                        return "The \(project) orchestrator call failed: \(message)"
                    }
                    return message
                }
            case .transport:
                return "Can't reach the cluster — is Tailscale connected? Your prompt is saved below; re-send when connected."
            case .invalidURL:
                return "The host or port is invalid. Set them in Settings."
            case .decoding(let detail):
                return "Unexpected response from the cluster: \(detail)"
            }
        }
        return "Something went wrong."
    }

    /// Build the configured client, or fail with a clear message when settings
    /// aren't set yet. This is an error path, not a silent fallback.
    private func clientOrThrow() throws -> HSCCClient {
        guard settings.isConfigured,
              let token = settings.token,
              let port = Int(settings.port) else {
            throw HSCCError.invalidURL
        }
        return HSCCClient(host: settings.host, port: port, token: token)
    }

    private func trimmed(_ s: String) -> String {
        s.trimmingCharacters(in: .whitespacesAndNewlines)
    }
}

// MARK: - Transcript model

/// One line in the chat transcript: a sent prompt, an orchestrator reply, or a
/// failed send. A failure is stored as its own case so it renders as a failure
/// (red), never as a reply. Rows are given stable identity by their index in
/// the append-only transcript, so this doesn't conform to Identifiable.
///
/// `Codable` so a per-project transcript can be persisted (`ChatStore`) across
/// navigation and app relaunch (requirement 4).
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

    var text: String {
        switch self {
        case .prompt(let t): return t
        case .reply(let t): return t
        case .failure(let t): return t
        case .unsent(let prompt, _): return prompt
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
        case prompt, reply, failure, unsent
    }

    private struct Payload: Codable {
        let kind: Kind
        let text: String
        /// Present only for `.unsent` — the reason delivery failed. Optional so
        /// older (pre-unsent) persisted records and reason-less unsents decode.
        var reason: String? = nil
    }

    init(from decoder: Decoder) throws {
        let payload = try Payload(from: decoder)
        switch payload.kind {
        case .prompt: self = .prompt(payload.text)
        case .reply: self = .reply(payload.text)
        case .failure: self = .failure(payload.text)
        case .unsent: self = .unsent(prompt: payload.text, reason: payload.reason)
        }
    }

    func encode(to encoder: Encoder) throws {
        let kind: Kind
        var reason: String? = nil
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
        }
        try Payload(kind: kind, text: text, reason: reason).encode(to: encoder)
    }
}

/// Bubble rendering for a transcript entry: prompts on the right (accent),
/// orchestrator replies on the left (system gray), failures in red, and UNSENT
/// messages in a muted bad-tint with a Retry button (t_c0953d4c).
private struct ChatBubble: View {
    let entry: ChatEntry
    /// Called when the operator taps Retry on an UNSENT message. The view
    /// passes this only for `.unsent` entries, and re-confirms before sending
    /// (no path around the confirm gate). When nil, no Retry is shown.
    var retry: (() -> Void)? = nil

    var body: some View {
        HStack(alignment: .bottom) {
            switch entry {
            case .prompt:
                Spacer(minLength: 48)
                bubble
                    .background(Color.accentColor)
                    .foregroundColor(.white)
            case .reply:
                bubble
                    .background(Color(.secondarySystemBackground))
                Spacer(minLength: 48)
            case .failure:
                bubble
                    .background(Theme.Semantic.bad.opacity(0.12))
                    .foregroundColor(Theme.Semantic.bad)
                Spacer(minLength: 48)
            case .unsent:
                // Left-ish like the other own-messages, but with a bad-tinted
                // background, an explicit UNSENT label, the failed text, the
                // delivery-failure reason (if any), and a Retry button. It is
                // ALWAYS visible — the failed message is never silently deleted.
                if let retry {
                    bubble
                        .background(Color.red.opacity(0.12))
                        .overlay(alignment: .bottomTrailing) { retryButton }
                } else {
                    bubble
                        .background(Color.red.opacity(0.12))
                }
                Spacer(minLength: 48)
            }
        }
    }

    private var retryButton: some View {
        Button {
            retry?()
        } label: {
            Label("Retry", systemImage: "arrow.clockwise")
                .font(.caption2.weight(.semibold))
                .padding(.horizontal, 8)
                .padding(.vertical, 4)
                .background(Color.red)
                .foregroundColor(.white)
                .clipShape(Capsule())
        }
        .buttonStyle(.borderless)
    }

    private var bubble: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label)
                .font(.caption2)
                .fontWeight(.semibold)
                .opacity(0.8)
            Text(entry.text)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
            // For an UNSENT message, surface why delivery failed (e.g. "can't
            // reach the cluster") so the operator knows what to fix before Retry.
            if let reason = entry.unsentReason {
                Text(reason)
                    .font(.caption2)
                    .opacity(0.85)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(10)
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
    }

    private var label: String {
        switch entry {
        case .prompt: return "YOU"
        case .reply: return "ORCHESTRATOR"
        case .failure: return "FAILED"
        case .unsent: return "UNSENT"
        }
    }
}
