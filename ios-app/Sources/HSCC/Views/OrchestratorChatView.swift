import SwiftUI

/// C5 — the orchestrator chat view.
///
/// Sends a prompt to a project's orchestrator via `POST /v1/orchestrator/chat`
/// and shows a prompt→reply transcript for the current app session.
///
/// SENDING IS A MUTATION: the orchestrator can decompose a prompt and dispatch
/// real work onto its board. So sending follows the SAME explicit confirm
/// pattern as every other mutating surface in the app (`MutationButton` /
/// `.confirmationDialog`): a tap on Send only ARMS the confirmation naming
/// exactly what will happen, and the request fires only after the user
/// confirms. There is no send-on-return and no other path that bypasses the
/// confirm step — this is the single place a chat request can fire.
///
/// Honest results: a non-2xx makes the client throw, which we append to the
/// transcript as a FAILURE with the API's message — never as a reply. A
/// "session not ready" 503 reads clearly (the orchestrator's named session must
/// exist first). A 504 timeout is surfaced as a timeout, not a silent empty
/// reply. Long orchestrator replies are expected, so an in-flight indicator
/// shows while we wait and the Send control is disabled (no double-tap
/// double-send).
struct OrchestratorChatView: View {
    @EnvironmentObject private var settings: SettingsStore

    /// The projects offered in the picker. The app has no `/v1/projects` list
    /// endpoint (checked the real API routes on feat/hscc-api), so it cannot
    /// fetch a live project list. `general` is the guaranteed catch-all
    /// orchestrator (`general-orch` / `general` session / `default` board) and
    /// is the default. If the API ever exposes a project-list endpoint, switch
    /// this to a fetch instead of a static list.
    static let knownProjects = ["general"]

    @State private var prompt = ""
    @State private var selectedProject = Self.knownProjects[0]
    @State private var transcript: [ChatEntry] = []
    @State private var showConfirm = false
    @State private var isSending = false

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
                .onChange(of: transcript.count) { _ in
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
        isSending ? "Waiting — an orchestrator can take a while." : nil
    }

    // MARK: - Composer (prompt + project picker + confirm-gated send)

    private var composer: some View {
        VStack(spacing: 8) {
            // Project picker — defaults to `general`.
            Picker("Project", selection: $selectedProject) {
                ForEach(Self.knownProjects, id: \.self) { project in
                    Text(project == Self.knownProjects.first ? "\(project) (default)" : project)
                        .tag(project)
                }
            }
            .pickerStyle(.menu)
            .disabled(isSending)

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
        "Send to the \(selectedProject) orchestrator?"
    }

    private var confirmMessage: String {
        "It may decompose your prompt and dispatch real work onto the \(selectedProject) project's board."
    }

    // MARK: - Send (confirm-gated)

    @MainActor
    private func send() async {
        let text = trimmed(prompt)
        // The transcript shows what was asked even if the send fails, so the
        // user can see the prompt that produced the failure. `.loading` is set
        // first so isSending disables the Send control (no double-fire).
        isSending = true
        defer { isSending = false }
        transcript.append(.prompt(text))

        do {
            let client = try clientOrThrow()
            let result = try await client.orchestratorChat(project: selectedProject,
                                                           prompt: text)
            transcript.append(.reply(result.reply))
            prompt = ""   // only clear on a real success
        } catch {
            // A non-2xx (400/409/502/503/504) makes the client throw. Render the
            // failure honestly — never as a reply. Map the transient states to
            // clear wording so each real condition reads distinctly.
            transcript.append(.failure(message(for: error)))
        }
    }

    /// Build a clear, human-facing failure string from the thrown error.
    /// Distinguishes the "session not ready" and "timeout" states from a generic
    /// failure so each real condition reads clearly.
    private func message(for error: Error) -> String {
        if let hscc = error as? HSCCError {
            switch hscc {
            case .api(let code, let message, let status):
                switch code {
                case "orchestrator_unavailable":
                    // A real state: the orchestrator's NAMED session must exist
                    // (created by provisioning / the first Telegram topic) before
                    // the orchestrator can be chatted with.
                    return "\(message) The \(selectedProject) orchestrator's session isn't ready yet — create it first, then re-send."
                case "orchestrator_timeout":
                    return "The \(selectedProject) orchestrator did not reply within 180 s (timeout). Try again or check the orchestrator."
                default:
                    // 400 unknown_project / bad_request, 409, 502 orchestrator_error, etc.
                    if status == 502 {
                        return "The orchestrator call failed: \(message)"
                    }
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
