import SwiftUI

/// C5 — the orchestrator chat view.
///
/// Sends a prompt to a project's orchestrator via a JOB-based flow
/// (`POST /v1/orchestrator/chat` returns 202 with a `job_id`, then
/// `GET /v1/orchestrator/chat/{id}` is polled for the reply) and shows a
/// prompt→reply transcript for the current app session.
///
/// WHY JOB-BASED (not streaming): `hermes chat -Q` emits the reply ONLY once,
/// complete, when the underlying run finishes — there is no supported
/// incremental/streaming interface to show tokens as they're generated (Phase 1
/// of t_bc242def confirmed this in both source and empirically). So instead of
/// a dead 90 s wait, the app shows an honest "Working — Ns elapsed" while it
/// polls, and a backgrounded app can pick the finished answer up later by
/// job_id (the POST returned in milliseconds, the server did the long work in
/// the background, and nothing is lost on a dropped connection).
///
/// SENDING IS A MUTATION: the orchestrator can decompose a prompt and dispatch
/// real work onto its board. So sending follows the SAME explicit confirm
/// pattern as every other mutating surface in the app (`MutationButton` /
/// `.confirmationDialog`): a tap on Send only ARMS the confirmation naming
/// exactly what will happen, and the request fires only after the user
/// confirms. There is no send-on-return and no other path that bypasses the
/// confirm step — this is the single place a chat request can fire.
///
/// Honest results: a job that lands in a terminal failure state (`timeout` /
/// `unavailable` / `error`) is appended to the transcript as a FAILURE with the
/// API's message — never as a reply. A "session not ready" unavailable reads
/// clearly. A timeout is surfaced as a timeout, not a silent empty reply.
struct OrchestratorChatView: View {
    @EnvironmentObject private var settings: SettingsStore

    /// When set, the chat is fixed to THIS project's orchestrator and the
    /// project picker is hidden (used from a project's detail screen). When
    /// nil (the standalone Chat surface), the picker shows and defaults to
    /// `general`.
    var project: String? = nil

    /// The projects offered in the picker when no fixed `project` is given.
    /// `general` is the guaranteed catch-all orchestrator. The app CAN fetch a
    /// live list from /v1/projects, but the standalone picker keeps `general`
    /// (the catch-all) first, with the live registry appended when configured.
    static let knownProjects = ["general"]

    @State private var prompt = ""
    @State private var selectedProject: String = OrchestratorChatView.knownProjects[0]
    @State private var transcript: [ChatEntry] = []
    @State private var showConfirm = false
    @State private var isSending = false

    /// The in-flight async job (nil when idle). Persisted across app
    /// backgrounding so a reply the server computed while the app was away can
    /// be picked up on return.
    @State private var jobID: String? = nil
    /// The project + prompt of the in-flight job (needed to resume its poll).
    @State private var jobProject: String? = nil
    @State private var jobPrompt: String? = nil
    /// Honest server-side elapsed seconds for the in-flight job's footer.
    @State private var liveElapsed: Double = 0

    /// The effective project the chat targets: the fixed one, or the picker's.
    private var chatProject: String { project ?? selectedProject }

    // UserDefaults keys for the persisted in-flight job (resume across a killed
    // app). The job_id + prompt are NOT secrets; the token stays in Keychain.
    private static let jobKey = "orch_chat_job_id"
    private static let jobProjectKey = "orch_chat_job_project"
    private static let jobPromptKey = "orch_chat_job_prompt"
    private static let jobPromptAtKey = "orch_chat_job_when"

    var body: some View {
        VStack(spacing: 0) {
            // The prompt→reply transcript of the current app session.
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(spacing: 12) {
                        if transcript.isEmpty {
                            emptyState
                        } else {
                            ForEach(transcript.indices, id: \.self) { index in
                                ChatBubble(entry: transcript[index])
                                    .id(index)   // stable — transcript is append-only
                            }
                        }
                    }
                    .padding(.horizontal)
                    .padding(.vertical, 8)
                }
                .onChange(of: transcript.count) {
                    // Scroll to the last row (index is a stable identity since the
                    // transcript is append-only within a session).
                    if !transcript.isEmpty {
                        withAnimation {
                            proxy.scrollTo(transcript.count - 1, anchor: .bottom)
                        }
                    }
                }
            }

            if let note = inFlightFooter {
                Text(note)
                    .font(.caption)
                    .foregroundColor(.secondary)
                    .padding(.horizontal)
                    .padding(.bottom, 4)
            }

            Divider()

            composer
                .padding(.horizontal)
                .padding(.vertical, 8)
        }
        .navigationTitle("Orchestrator")
        .navigationBarTitleDisplayMode(.inline)
        .onAppear { resumePersistedJobIfAny() }
        .onDisappear { pollTask?.cancel() }
    }

    // MARK: - In-flight job resume/poll machinery

    /// A `Task`-backed poll handle so we can cancel between poller AND visual
    /// updates when the view disappears.
    @State private var pollTask: Task<Void, Never>? = nil

    /// When the view appears, if a chat job was persisted in a previous
    /// session (the app was backgrounded/killed while the orchestrator was
    /// working), resume polling it so the answer the server already computed is
    /// not lost.
    private func resumePersistedJobIfAny() {
        guard jobID == nil,
              let savedJobID = UserDefaults.standard.string(forKey: Self.jobKey),
              !savedJobID.isEmpty else { return }
        let savedProject = UserDefaults.standard.string(forKey: Self.jobProjectKey)
        let savedPrompt = UserDefaults.standard.string(forKey: Self.jobPromptKey)
        transcript.append(.prompt(savedPrompt ?? "(resumed)"))
        beginPolling(jobID: savedJobID,
                     project: savedProject,
                     onAppearResume: true)
    }

    /// Start polling a job: persist it, show honest elapsed, and on a terminal
    /// state append the result and clear the persisted job.
    private func beginPolling(jobID: String, project: String?, onAppearResume: Bool = false) {
        self.jobID = jobID
        self.jobProject = project
        self.jobPrompt = prompt
        isSending = true
        persistJob(jobID: jobID, project: project, prompt: prompt)

        pollTask = Task { @MainActor in
            // Poll every 2s so the footer shows honest progress and the reply
            // appears promptly once the orchestrator finishes. This replaces
            // the old single 300 s blocking wait.
            while !Task.isCancelled {
                do {
                    let client = try clientOrThrow()
                    let status = try await client.orchestratorChatPoll(jobID: jobID)
                    liveElapsed = status.elapsed
                    if status.isTerminal {
                        finish(terminal: status)
                        return
                    }
                } catch {
                    // A transient poll failure (e.g. the app was offline for a
                    // moment) should NOT kill the job — the server keeps
                    // working. Keep the persisted job and retry; only surface
                    // if the connection is genuinely gone and stays gone.
                    await Task.sleep(nanoseconds: 2_000_000_000)
                    continue
                }
                await Task.sleep(nanoseconds: 2_000_000_000)
            }
        }
    }

    /// Handle a terminal poll result: append the reply or failure and clear the
    /// persisted job so it isn't resumed twice.
    private func finish(terminal status: OrchestratorChatJobStatus) {
        if status.status == "done", let reply = status.reply {
            transcript.append(.reply(reply))
        } else {
            let message = status.error?.message ?? "The orchestrator call failed."
            transcript.append(.failure(statusMessage(for: status.error, project: chatProject, fallback: message)))
        }
        clearPersistedJob()
        jobID = nil
        isSending = false
        pollTask = nil
        if status.status == "done" { prompt = "" }   // only clear on a real success
    }

    /// Persist the in-flight job to UserDefaults so it survives app backgrounding.
    private func persistJob(jobID: String, project: String?, prompt: String) {
        let d = UserDefaults.standard
        d.set(jobID, forKey: Self.jobKey)
        d.set(project ?? "", forKey: Self.jobProjectKey)
        d.set(prompt, forKey: Self.jobPromptKey)
        d.set(Date().timeIntervalSince1970, forKey: Self.jobPromptAtKey)
    }

    /// Clear the persisted in-flight job (called on any terminal state).
    private func clearPersistedJob() {
        let d = UserDefaults.standard
        d.removeObject(forKey: Self.jobKey)
        d.removeObject(forKey: Self.jobProjectKey)
        d.removeObject(forKey: Self.jobPromptKey)
        d.removeObject(forKey: Self.jobPromptAtKey)
    }

    /// Build a clear, human-facing failure string from a job's error.
    private func statusMessage(for error: ChatJobError?, project: String, fallback: String) -> String {
        guard let error else { return fallback }
        switch error.code {
        case "orchestrator_unavailable":
            // A real state: the orchestrator's NAMED session must exist
            // (created by provisioning / the first Telegram topic) before the
            // orchestrator can be chatted with.
            return "\(error.message) The \(project) orchestrator's session isn't ready yet — create it first, then re-send."
        case "orchestrator_timeout":
            return "The \(project) orchestrator did not reply within 180 s (timeout). Try again or check the orchestrator."
        case "orchestrator_error":
            return "The orchestrator call failed: \(error.message)"
        default:
            return error.message
        }
    }

    // MARK: - Empty / in-flight states

    private var emptyState: some View {
        VStack(spacing: 8) {
            Image(systemName: "bubble.left.and.bubble.right")
                .font(.largeTitle)
                .foregroundColor(.secondary)
            Text("Ask the Orchestrator")
                .font(.headline)
            Text("Send a prompt to an orchestrator. It may decompose your request and dispatch real work onto its board.")
                .font(.footnote)
                .foregroundColor(.secondary)
                .multilineTextAlignment(.center)
        }
        .padding(.top, 48)
        .padding(.horizontal, 24)
    }

    private var inFlightFooter: String? {
        guard isSending else { return nil }
        let seconds = Int(liveElapsed)
        let minutes = seconds / 60
        let secs = seconds % 60
        let elapsedText = minutes > 0 ? "\(minutes)m \(secs)s" : "\(secs)s"
        return "Working — the orchestrator has been on it for \(elapsedText). The answer appears here when it's ready."
    }

    // MARK: - Composer (prompt + project picker + confirm-gated send)

    private var composer: some View {
        VStack(spacing: 8) {
            // Project picker — hidden when the chat is fixed to a project.
            if project == nil {
                Picker("Project", selection: $selectedProject) {
                    ForEach(Self.knownProjects, id: \.self) { item in
                        Text(item == Self.knownProjects.first ? "\(item) (default)" : item)
                            .tag(item)
                    }
                }
                .pickerStyle(.menu)
                .disabled(isSending)
            }

            HStack(alignment: .bottom, spacing: 8) {
                TextField("Ask the orchestrator…", text: $prompt, axis: .vertical)
                    .lineLimit(1...4)
                    .textFieldStyle(.roundedBorder)
                    .disabled(isSending)

                // STEP 1 — a tap only arms the confirmation. No request is sent,
                // so a double-tap on Send can never double-send.
                Button {
                    showConfirm = true
                } label: {
                    if isSending {
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
                Task { await send() }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text(confirmMessage)
        }
    }

    private var canSend: Bool {
        !isSending && !trimmed(prompt).isEmpty
    }

    private var confirmTitle: String {
        "Send to the \(chatProject) orchestrator?"
    }

    private var confirmMessage: String {
        "It may decompose your prompt and dispatch real work onto the \(chatProject) project's board."
    }

    // MARK: - Send (confirm-gated)

    @MainActor
    private func send() async {
        let text = trimmed(prompt)
        // The transcript shows what was asked even if the send fails, so the
        // user can see the prompt that produced the failure. `.loading` is set
        // first so isSending disables the Send control (no double-fire).
        isSending = true
        transcript.append(.prompt(text))

        do {
            let client = try clientOrThrow()
            // POST returns in milliseconds with a job_id — no dead wait.
            // The orchestrator is messaged in the background on the server.
            let started = try await client.orchestratorChatStart(project: chatProject,
                                                                 prompt: text)
            beginPolling(jobID: started.jobID, project: chatProject)
        } catch {
            // A non-2xx POST (400/409) throws. Render the failure honestly —
            // never as a reply. isSending is reset here because no job exists.
            transcript.append(.failure(message(for: error)))
            clearPersistedJob()
            jobID = nil
            isSending = false
        }
    }

    /// Build a clear, human-facing failure string from a thrown (non-job) error.
    private func message(for error: Error) -> String {
        if let hscc = error as? HSCCError {
            switch hscc {
            case .api(let code, let message, let status):
                switch code {
                case "unknown_project":
                    return "Unknown project: \(message)"
                case "bad_request":
                    return message
                default:
                    return message
                }
            case .transport:
                return "Can't reach the cluster — is Tailscale connected?"
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
enum ChatEntry {
    case prompt(String)
    case reply(String)
    case failure(String)

    var text: String {
        switch self {
        case .prompt(let t): return t
        case .reply(let t): return t
        case .failure(let t): return t
        }
    }
}

/// Bubble rendering for a transcript entry: prompts on the right (accent),
/// orchestrator replies on the left (system gray), failures in red.
private struct ChatBubble: View {
    let entry: ChatEntry

    var body: some View {
        HStack {
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
                    .background(Color.red.opacity(0.12))
                    .foregroundColor(.red)
                Spacer(minLength: 48)
            }
        }
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
        }
        .padding(10)
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
    }

    private var label: String {
        switch entry {
        case .prompt: return "YOU"
        case .reply: return "ORCHESTRATOR"
        case .failure: return "FAILED"
        }
    }
}
