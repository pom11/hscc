import SwiftUI

// ===========================================================================
// StreamingChatView — the LIVE chat: a window onto the project's session.
//
// Unlike the old job-based chat (which polled for a single reply string), this
// renders the TYPED event stream in real time. Each event type has a distinct
// treatment, per the streaming-chat card requirements:
//
//   * message  — a bubble that STREAMS token-by-token as deltas land (the
//                transcript aggregates the deltas; this view just draws the
//                latest composed text, so it animates naturally).
//   * tool_call— a COLLAPSED one-liner chip (`read kanban — 0.4s`), tap to
//                expand to its args/result. Paired start+finish are ONE chip.
//   * card     — a tappable chip (→ CardDetailView), NOT prose.
//   * agent    — a subagent spawned/finished, showing its role.
//   * system   — an ambient session fact (cron fired, crash, escalation).
//   * error    — a named, actionable failure.
//   * notice   — client-local framing (reconnected, gap filled).
//
// The status banner always says whether the operator is seeing a LIVE stream
// or a static/stale transcript. Everything uses Theme tokens — no raw hex.
// ===========================================================================

struct StreamingChatView: View {
    let project: String

    @EnvironmentObject private var settings: SettingsStore
    @EnvironmentObject private var unread: ProjectUnreadCenter
    @EnvironmentObject private var replyWatcher: StreamReplyWatcher
    @StateObject private var store: StreamingChatStore
    /// Drives the session Live Activity (lock screen / Dynamic Island mirror
    /// of this project's live session). A plain `let` — it holds no published
    /// state, it just reflects changes when the stream updates.
    private let sessionActivity = SessionActivityDriver()

    /// Focus of the composer TextField. The Voice button sets this true to
    /// summon the keyboard and its built-in SYSTEM DICTATION key — the chat's
    /// voice-input affordance (a custom speech pipeline would be more code and
    /// a fresh permission story; system dictation is the OS-owned one-tap
    /// path and is exactly what the operator reaches for one-handed / in-car).
    @FocusState private var composerFocused: Bool

    init(project: String) {
        self.project = project
        // `project` is captured; the @StateObject is created here so the store
        // (and its persisted cursor/draft) is stable across body recomputes.
        _store = StateObject(wrappedValue: StreamingChatStore(project: project))
    }

    var body: some View {
        VStack(spacing: 0) {
            statusBanner
                .padding(.horizontal)
                .padding(.top, Theme.Spacing.sm.rawValue)

            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(spacing: Theme.Spacing.md.rawValue) {
                        if store.rows.isEmpty {
                            if case .failed = store.phase {
                                failedState
                                    .padding(.top, 64)
                            } else {
                                emptyState
                                    .padding(.top, 64)
                            }
                        } else {
                            ForEach(store.rows) { row in
                                rowView(row)
                                    .id(row.id)
                            }
                        }
                    }
                    .padding(.horizontal)
                    .padding(.vertical, Theme.Spacing.md.rawValue)
                }
                // Stream to the bottom as the live tail grows: fires when a
                // new row lands AND when the last row is a message whose text
                // grows token-by-token. This is a live-stream view — the
                // primary mode is watching the tail (SessionHistoryView is the
                // pager for reading back).
                .onChange(of: liveTick) { _, _ in
                    withAnimation(.easeOut(duration: 0.15)) {
                        if let last = store.rows.last {
                            proxy.scrollTo(last.id, anchor: .bottom)
                        }
                    }
                }
            }

            if let err = store.sendError {
                Text(err)
                    .font(.footnote)
                    .foregroundColor(Theme.Semantic.bad)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal)
                    .padding(.bottom, Theme.Spacing.xxs.rawValue)
            }

            Divider()
            composer
                .padding(.horizontal)
                .padding(.vertical, Theme.Spacing.sm.rawValue)
        }
        .onAppear {
            // Opening this chat declares it the reading project (suppresses
            // badging a reply that lands while it is on screen) and clears any
            // badge that was waiting. Wire the live-stream watermark to the
            // shared watcher so replies seen here are never re-badged later.
            unread.setReading(project)
            unread.markRead(project: project)
            startStream()
        }
        .onDisappear {
            store.persistDraft()
            store.stop()
            // No longer reading this chat — a reply that lands now must badge.
            unread.setReading(nil)
            // Leaving the live chat ends the lock-screen mirror.
            sessionActivity.end()
        }
        // Reflect stream changes onto the lock screen Live Activity: whenever
        // the connection phase changes or more rows/streaming text land, push
        // the derived summary so the Dynamic Island stays current.
        .onChange(of: store.phase) { _, phase in
            reflectSessionActivity(phase: phase)
        }
        .onChange(of: liveTick) { _, _ in
            reflectSessionActivity(phase: store.phase)
        }
    }

    /// Push the current rows + phase to the session Live Activity driver.
    private func reflectSessionActivity(phase: ConnectionPhase) {
        sessionActivity.reflect(project: project, rows: store.rows, phase: phase)
    }

    private func startStream() {
        // Always hand settings to the store — even unconfigured — so it can
        // surface an honest `.failed` state instead of silently staying idle.
        let port = Int(settings.port) ?? 0
        let s = StreamSettings(host: settings.host, port: port,
                               token: settings.token ?? "",
                               isConfigured: settings.isConfigured)
        store.onEvent = { [weak replyWatcher] seq in
            replyWatcher?.noteSeen(project: project, seq: seq)
        }
        Task { await store.start(settings: s) }
    }

    // MARK: - Status banner

    @ViewBuilder
    private var statusBanner: some View {
        let (text, color, icon) = bannerContent(store.phase)
        HStack(spacing: Theme.Spacing.sm.rawValue) {
            Image(systemName: icon)
                .font(.caption)
            Text(text)
                .font(.caption)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.horizontal, Theme.Spacing.sm.rawValue)
        .padding(.vertical, Theme.Spacing.xs.rawValue)
        .background(color.opacity(0.12))
        .clipShape(RoundedRectangle(cornerRadius: Theme.Corner.badge.rawValue, style: .continuous))
    }

    private func bannerContent(_ phase: ConnectionPhase) -> (String, Color, String) {
        switch phase {
        case .idle:
            return ("Starting the session stream…", Theme.Semantic.neutral, "antenna.radiowaves.left.and.right")
        case .loadingHistory:
            return ("Loading session history…", Theme.Semantic.neutral, "tray.and.arrow.down")
        case .connecting:
            return ("Connecting to the live stream…", Theme.Semantic.warn, "antenna.radiowaves.left.and.right")
        case .connected:
            return ("Live — watching \(project)'s session.", Theme.Semantic.ok, "dot.radiowaves.left.and.right")
        case .reconnecting:
            return ("Reconnecting… resuming from latest, no gap.", Theme.Semantic.warn, "arrow.clockwise")
        case .failed(let reason):
            return (reason, Theme.Semantic.bad, "exclamationmark.triangle")
        }
    }

    // MARK: - Rows

    /// A cheap Equatable signature of the live tail — changes when a new row
    /// lands OR when the last row streams more text. Drives auto-scroll.
    private var liveTick: String {
        guard let last = store.rows.last else { return "empty" }
        if case .message(_, let text, _) = last.item { return "\(last.id)|\(text.count)" }
        return last.id
    }

    @ViewBuilder
    private func rowView(_ row: ChatRow) -> some View {
        switch row.item {
        case .message(let role, let text, let streaming):
            MessageBubble(role: role, text: text, streaming: streaming)
        case .tool(let t):
            ToolChip(render: t, rowID: row.id, store: store)
        case .card(let c):
            CardChip(card: c)
        case .agent(let a):
            AgentRow(agent: a)
        case .system(let s):
            SystemRow(system: s)
        case .error(let e):
            ErrorRow(error: e)
        case .unknown(let type, let raw):
            UnknownRow(type: type, raw: raw)
        case .notice(let text):
            NoticeRow(text: text)
        }
    }

    private var emptyState: some View {
        VStack(spacing: Theme.Spacing.sm.rawValue) {
            Image(systemName: "bubble.left.and.bubble.right")
                .font(.largeTitle)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
            Text("Watching \(project)'s session")
                .font(.headline)
            Text("This is a live window onto the whole session — the orchestrator's messages, its tool calls, the cards it moves, and the subagents it spawns. Send a prompt below.")
                .font(.footnote)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 24)
        }
    }

    /// Distinct state for a stream that COULD NOT load — shown instead of the
    /// live-chat invite so a failure never masquerades as an idle, working
    /// chat. The banner repeats the reason, but the body here makes the dead
    /// state unmistakable (the two must not look the same).
    private var failedState: some View {
        VStack(spacing: Theme.Spacing.sm.rawValue) {
            Image(systemName: "exclamationmark.triangle")
                .font(.largeTitle)
                .foregroundColor(Theme.Semantic.bad)
            Text("Couldn't connect to the session stream")
                .font(.headline)
            if case .failed(let reason) = store.phase {
                Text(reason)
                    .font(.footnote)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, 24)
            }
            Text("Check Settings → Host, Port, and Token, then pull to reconnect.")
                .font(.footnote)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 24)
        }
    }

    // MARK: - Composer

    private var composer: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs.rawValue) {
            // Slash-command palette (server-sourced command catalog) at a "/"
            // command position; selecting a command inserts it into the draft.
            SlashCommandPalette(draft: $store.draft, client: computedClient)

            HStack(alignment: .bottom, spacing: Theme.Spacing.sm.rawValue) {
                TextField("Ask \(project)…", text: $store.draft, axis: .vertical)
                    .lineLimit(1...4)
                    .textFieldStyle(.roundedBorder)
                    .focused($composerFocused)
                    .onChange(of: store.draft) { _, _ in
                        store.persistDraft()
                    }

                // Voice / Dictate — focuses the composer field so the system
                // keyboard (with its built-in dictation key) comes up. Tapping
                // the system dictation key captures the operator's speech into
                // this draft. This is the "system dictation affordance" the card
                // asks for: no custom speech pipeline, no new app-level
                // permission (the keyboard manages the microphone itself).
                Button {
                    startDictation()
                } label: {
                    Image(systemName: "mic.fill")
                        .frame(width: 24, height: 24)
                }
                .buttonStyle(.bordered)
                .accessibilityLabel("Start dictation")
                .accessibilityHint("Focuses the message field and opens the keyboard's dictation key.")

                Button {
                    // Pressing send sends — chat messages are neither destructive
                    // nor expensive, so there is no confirm gate. `send` adds the
                    // optimistic user row immediately and clears the composer.
                    store.send(store.draft)
                } label: {
                    Image(systemName: "paperplane.fill")
                        .frame(width: 24, height: 24)
                }
                .buttonStyle(.borderedProminent)
                .disabled(!canSend)
                .accessibilityLabel("Send")
            }
            // Quiet context near the composer (not a modal gate): which session
            // the message lands in.
            Text("Sends to \(project)'s session — its tool calls, card moves, and reply stream back here.")
                .font(.caption)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
        }
    }

    /// The client used by the slash palette, from current settings (nil when
    /// unconfigured — the palette then stays hidden; no hardcoded command list).
    private var computedClient: HSCCClient? {
        guard settings.isConfigured,
              let token = settings.token,
              let port = Int(settings.port) else { return nil }
        return HSCCClient(host: settings.host, port: port, token: token)
    }

    private var canSend: Bool {
        !ComposerText.isEmpty(store.draft)
    }

    /// The Voice button's action. There is no public API to launch the system
    /// dictation UI directly, so we focus the composer field: that summons the
    /// system keyboard with its built-in dictation key, which the operator
    /// taps next (one further tap, one-handed / in-car friendly). The recognized
    /// text lands in `store.draft` through the binding; draft shaping is owned
    /// by the pure, harness-tested `ComposerText` helpers.
    private func startDictation() {
        composerFocused = true
    }

    private func trimmed(_ s: String) -> String {
        ComposerText.sendable(s)
    }
}

// MARK: - Message bubble

/// A message turn: the orchestrator's reply (or the operator's echo). Streams
/// while `streaming == true` — the text grows as deltas land and a subtle
/// caret signals it is still being typed.
///
/// Operator vs assistant must be tellable at a glance, so the two use OPPOSITE
/// alignment and OPPOSITE backgrounds (the same recipe OrchestratorChatView
/// ships):
///  * the operator's echo sits RIGHT on a solid accent field with white text,
///    tagged "YOU";
///  * the orchestrator sits LEFT on a raised gray field with primary text,
///    tagged "ORCHESTRATOR".
/// Each bubble carries a TURN MARKER (the role label) and real vertical
/// separation (turnGap) so the transcript reads as distinct turns rather than
/// a wall of same-weight text. Long replies are capped to a readable measure
/// (chatMeasure) so text never runs edge-to-edge.
private struct MessageBubble: View {
    let role: String
    let text: String
    let streaming: Bool

    private var isUser: Bool { role == "user" }

    var body: some View {
        HStack(alignment: .bottom, spacing: 0) {
            if isUser {
                Spacer(minLength: 56)
                bubble
            } else {
                bubble
                Spacer(minLength: 40)
            }
        }
        .frame(maxWidth: .infinity)
        // Real vertical separation between turns — builds on top of the
        // LazyVStack base spacing so a new turn never blurs into the row above.
        .padding(.top, turnGap)
    }

    private var bubble: some View {
        VStack(alignment: isUser ? .trailing : .leading, spacing: Theme.Spacing.xxs.rawValue) {
            // Turn marker — the operator can tell WHO said it at a glance, and
            // that a new turn began here.
            Text(isUser ? "YOU" : "ORCHESTRATOR")
                .font(.caption2.weight(.semibold))
                .textCase(.uppercase)
                .foregroundColor(isUser ? accentRoleColor : Theme.Semantic.onSurfaceMuted)
            Text(text + (streaming ? " ▍" : ""))
                .font(.body)
                .foregroundColor(textColor)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: isUser ? .trailing : .leading)
        }
        .padding(.horizontal, Theme.Spacing.md.rawValue)
        .padding(.vertical, Theme.Spacing.sm.rawValue)
        .frame(maxWidth: chatMeasure, alignment: isUser ? .trailing : .leading)
        .background(
            RoundedRectangle(cornerRadius: Theme.Corner.card.rawValue, style: .continuous)
                .fill(streamBubbleColor)
        )
        .overlay(
            RoundedRectangle(cornerRadius: Theme.Corner.card.rawValue, style: .continuous)
                .stroke(outlineColor, lineWidth: 1)
        )
    }

    /// A readable measure for long replies — the bubble never spans the full
    /// width edge-to-edge, even on a wide screen (the iPhone shorter leg just
    /// keeps the Spacer approach; this cap is what protects iPad/large text).
    private var chatMeasure: CGFloat { 560 }

    /// Breathing room above each new turn so turns don't blur together.
    private var turnGap: CGFloat { Theme.Spacing.md.rawValue }

    /// The role label over a SOLID accent field is white for contrast — the
    /// established OrchestratorChat pattern (white on fixed accent, readable
    /// in both appearances).
    private var accentRoleColor: Color {
        .white  // theme-allow: white on the solid accent role tag
    }

    private var textColor: Color {
        if isUser { return .white /* theme-allow: white on solid accent, readable in both appearances */ }
        return Theme.Semantic.onSurface
    }

    private var streamBubbleColor: Color {
        if isUser {
            // Solid accent, not a pale tint — the operator's own message must
            // not blend into the raised gray the assistant uses.
            return Color.accentColor
        }
        return streaming ? Theme.Semantic.surfaceRaised : Theme.Semantic.surfaceElevated
    }

    private var outlineColor: Color {
        if isUser { return Color.accentColor }
        return streaming ? Theme.Semantic.ok.opacity(0.6) : .clear
    }
}

// MARK: - Tool chip

/// A tool call: ONE collapsed one-liner (`read kanban — 0.4s`), tap to expand
/// its args/result. `start`+`finish` are already composed into one ToolRender
/// by the transcript, so this is a single chip, not two rows.
private struct ToolChip: View {
    let render: ToolRender
    let rowID: String
    @ObservedObject var store: StreamingChatStore

    private var isExpanded: Bool { store.expandedToolIDs.contains(rowID) }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button {
                withAnimation(.easeInOut(duration: 0.15)) { store.toggleTool(rowID) }
            } label: {
                HStack(spacing: Theme.Spacing.sm.rawValue) {
                    Image(systemName: icon)
                        .font(.caption)
                        .foregroundColor(color)
                    VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
                        HStack(spacing: Theme.Spacing.xs.rawValue) {
                            Text(render.name)
                                // Machine metadata — quieter than prose so a
                                // collapsed tool call reads as a side-note,
                                // not a competing conversational turn.
                                .font(.subheadline)
                                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                            if let dur = render.duration {
                                Text(String(format: "%.1fs", dur))
                                    .font(.caption)
                                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
                            }
                        }
                        if !isExpanded, let summary = collapsedSummary {
                            Text(summary)
                                .font(.caption)
                                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                                .lineLimit(1)
                        }
                    }
                    Spacer()
                    Image(systemName: isExpanded ? "chevron.up" : "chevron.down")
                        .font(.caption2)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
                .padding(.horizontal, Theme.Spacing.md.rawValue)
                .padding(.vertical, Theme.Spacing.sm.rawValue)
            }
            .buttonStyle(.plain)

            if isExpanded {
                Divider()
                expandPanel
            }
        }
        .background(
            RoundedRectangle(cornerRadius: Theme.Corner.card.rawValue, style: .continuous)
                .fill(Theme.Semantic.surfaceElevated)
        )
    }

    private var icon: String {
        if render.finished { return render.duration != nil ? "checkmark.circle" : "hammer" }
        return "circle.dotted"
    }

    private var color: Color {
        render.finished ? Theme.Semantic.ok : Theme.Semantic.warn
    }

    /// A tight one-line summary of what the tool did for the collapsed chip.
    private var collapsedSummary: String? {
        if let result = render.result {
            let v = result.renderInline()
            if !v.isEmpty { return v }
        }
        if let args = render.args, !args.isEmpty {
            return args.renderArgs(pretty: false)
        }
        return nil
    }

    private var expandPanel: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.sm.rawValue) {
            if let args = render.args, !args.isEmpty {
                panelSection("Arguments") {
                    Text(args.renderArgs(pretty: true))
                        .font(.hsccMono(12))
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            if let result = render.result {
                panelSection(render.finished ? "Result" : "…") {
                    Text(result.renderInline())
                        .font(.hsccMono(12))
                        .foregroundColor(Theme.Semantic.onSurface)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            if !render.finished {
                HStack(spacing: Theme.Spacing.xs.rawValue) {
                    ProgressView()
                        .controlSize(.small)
                    Text("running…")
                        .font(.caption)
                        .foregroundColor(Theme.Semantic.warn)
                }
            }
        }
        .padding(.horizontal, Theme.Spacing.md.rawValue)
        .padding(.vertical, Theme.Spacing.sm.rawValue)
    }

    private func panelSection(_ title: String,
                              @ViewBuilder content: () -> some View) -> some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
            Text(title)
                .font(.caption.weight(.semibold))
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
            content()
        }
    }
}

// MARK: - Card chip

/// A kanban change rendered as a TAPPABLE chip (not prose) — jumps to the
/// card's detail. The chip shows board + the state change + title.
private struct CardChip: View {
    let card: CardPayload

    var body: some View {
        NavigationLink {
            CardDetailView(cardID: card.id)
        } label: {
            HStack(spacing: Theme.Spacing.sm.rawValue) {
                Image(systemName: "square.grid.2x2")
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.neutral)
                VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
                    Text(card.title)
                        // Card moves are metadata under the conversation —
                        // quieter than prose so they don't compete for the eye.
                        .font(.subheadline)
                        .foregroundColor(Theme.Semantic.onSurface)
                        .lineLimit(2)
                    HStack(spacing: Theme.Spacing.xs.rawValue) {
                        Text(card.board)
                            .font(.caption2)
                            .foregroundColor(Theme.Semantic.onSurfaceMuted)
                        Text("·")
                            .font(.caption2)
                            .foregroundColor(Theme.Semantic.onSurfaceMuted)
                        Text(card.status)
                            .font(.caption2.weight(.semibold))
                            .foregroundColor(statusColor(card.status))
                    }
                }
                Spacer()
                Image(systemName: "chevron.right")
                    .font(.caption2)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
            .padding(.horizontal, Theme.Spacing.md.rawValue)
            .padding(.vertical, Theme.Spacing.sm.rawValue)
        }
        .buttonStyle(.plain)
        .background(
            RoundedRectangle(cornerRadius: Theme.Corner.card.rawValue, style: .continuous)
                .fill(Theme.Semantic.surfaceElevated)
        )
    }

    private func statusColor(_ status: String) -> Color {
        switch status {
        case "done", "closed", "merged", "completed": return Theme.Semantic.ok
        case "blocked": return Theme.Semantic.bad
        case "running", "in_progress", "ready": return Theme.Semantic.warn
        default: return Theme.Semantic.neutral
        }
    }
}

// MARK: - Agent row

/// A subagent spawned or finished, showing its role (profile name).
private struct AgentRow: View {
    let agent: AgentPayload

    var body: some View {
        HStack(spacing: Theme.Spacing.sm.rawValue) {
            Image(systemName: agent.action == "finished" ? "person.fill.checkmark" : "person.badge.plus")
                .font(.caption)
                .foregroundColor(Theme.Semantic.ok)
            VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
                HStack(spacing: Theme.Spacing.xs.rawValue) {
                    Text(agent.role)
                        .font(.subheadline.weight(.semibold))
                        .foregroundColor(Theme.Semantic.onSurface)
                    Text(agent.action)
                        .font(.caption)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
                if let task = agent.task, !task.isEmpty {
                    Text(task)
                        .font(.caption)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                        .lineLimit(1)
                }
            }
            Spacer()
        }
        .padding(.horizontal, Theme.Spacing.md.rawValue)
        .padding(.vertical, Theme.Spacing.sm.rawValue)
        .background(
            RoundedRectangle(cornerRadius: Theme.Corner.card.rawValue, style: .continuous)
                .fill(Theme.Semantic.surfaceRaised)
        )
    }
}

// MARK: - System row

/// An ambient session fact: cron fired, worker crash, escalation, compaction.
/// Reads quietly (muted) so it does not shout over the actual conversation.
private struct SystemRow: View {
    let system: SystemPayload

    var body: some View {
        HStack(spacing: Theme.Spacing.sm.rawValue) {
            Image(systemName: systemIcon)
                .font(.caption)
                .foregroundColor(Theme.Semantic.neutral)
            VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
                Text(system.kind)
                    .font(.caption.weight(.semibold))
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    .textCase(.uppercase)
                if let d = system.details, !d.isEmpty {
                    Text(d.renderArgs(pretty: false))
                        .font(.caption2)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                        .lineLimit(2)
                }
            }
            Spacer()
        }
        .padding(.horizontal, Theme.Spacing.md.rawValue)
        .padding(.vertical, Theme.Spacing.xs.rawValue)
        .frame(maxWidth: .infinity)
        .background(
            RoundedRectangle(cornerRadius: Theme.Corner.badge.rawValue, style: .continuous)
                .fill(Theme.Semantic.surfaceRaised)
        )
    }

    private var systemIcon: String {
        switch system.kind {
        case "cron_fired": return "clock"
        case "worker_crash": return "exclamationmark.octagon"
        case "escalation": return "arrow.up.forward.square"
        case "compaction": return "arrow.down.left.and.arrow.up.right"
        case "session_rotated": return "arrow.triangle.2.circlepath"
        default: return "gearshape"
        }
    }
}

// MARK: - Error row

/// A named, actionable failure. The name (`code`) is shown so the operator
/// can recognize and fix it; the message says what to do next.
private struct ErrorRow: View {
    let error: ErrorPayload

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs.rawValue) {
            HStack(spacing: Theme.Spacing.sm.rawValue) {
                Image(systemName: "exclamationmark.triangle.fill")
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.bad)
                Text(error.code)
                    .font(.subheadline.weight(.semibold))
                    .foregroundColor(Theme.Semantic.bad)
                Spacer()
            }
            Text(error.message)
                .font(.footnote)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.horizontal, Theme.Spacing.md.rawValue)
        .padding(.vertical, Theme.Spacing.sm.rawValue)
        .background(
            RoundedRectangle(cornerRadius: Theme.Corner.card.rawValue, style: .continuous)
                .fill(Theme.Semantic.bad.opacity(0.10))
        )
        .overlay(
            RoundedRectangle(cornerRadius: Theme.Corner.card.rawValue, style: .continuous)
                .stroke(Theme.Semantic.bad.opacity(0.4), lineWidth: 1)
        )
    }
}

// MARK: - Unknown row

/// An event type this build does not know (newer backend). Surfaces the raw
/// text so nothing is dropped silently.
private struct UnknownRow: View {
    let type: String
    let raw: String

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs.rawValue) {
            HStack(spacing: Theme.Spacing.sm.rawValue) {
                Image(systemName: "questionmark.circle")
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.neutral)
                Text("Unknown event: \(type)")
                    .font(.caption.weight(.semibold))
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
                Spacer()
            }
            Text(raw)
                .font(.hsccMono(11))
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                .textSelection(.enabled)
        }
        .padding(.horizontal, Theme.Spacing.md.rawValue)
        .padding(.vertical, Theme.Spacing.sm.rawValue)
        .background(
            RoundedRectangle(cornerRadius: Theme.Corner.badge.rawValue, style: .continuous)
                .fill(Theme.Semantic.surfaceRaised)
        )
    }
}

// MARK: - Notice row

/// Client-local framing — a reconnect, a filled gap, a persisted-cursor
/// resumption. Never a server event.
private struct NoticeRow: View {
    let text: String

    var body: some View {
        HStack(spacing: Theme.Spacing.sm.rawValue) {
            Image(systemName: "arrow.uturn.forward")
                .font(.caption)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
            Text(text)
                .font(.caption)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.vertical, Theme.Spacing.xs.rawValue)
    }
}
