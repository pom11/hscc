import SwiftUI

// ===========================================================================
// StreamingChatView — the LIVE chat: a window onto the project's session.
//
// FULL-BLEED (IOS_REDESIGN 2026-10-14 §4): the 3 stacked banners and the
// boxed-row cards are gone. The transcript owns all remaining height; rows
// read as hairline-separated lines, not floating cards. What changed:
//
//   * status banner          -> a 9 pt live dot in the nav bar (tap = status)
//   * turn-state banner      -> one 24 pt pill pinned above the composer,
//                               only while a turn runs
//   * boxed rows             -> hairline rows (hairline top, 16 gutters)
//   * operator message       -> the ONLY filled bubble in the app (accent,
//                               onAccent text, squared bottom-trailing corner)
//   * REPL reply             -> plain full-width text — "the terminal talking"
//   * system kind=reasoning  -> NEW collapsible Reasoning row (it used to
//                               drown in SystemRow's caption2 lineLimit(2))
//   * via=tmux/serve         -> centred notice row (was a details dump)
//   * ToolChip               -> same recipe + same store-side expandedToolIDs,
//                               only the card background/stroke removed
//   * per-token animated scrollTo -> stick-to-bottom gate: follow only within
//                               ~80 pt of the bottom; otherwise a "N new ↓"
//                               pill; Reduce Motion scrolls instantly
//   * composer               -> safeAreaInset(edge: .bottom) so the transcript
//                               reflows above the keyboard
//
// The typed event stream itself (StreamingChatStore / StreamingTranscript /
// SessionEvent) is untouched: this file only draws the rows the store emits.
// ===========================================================================

struct StreamingChatView: View {
    let project: String

    @EnvironmentObject private var settings: SettingsStore
    @EnvironmentObject private var unread: ProjectUnreadCenter
    @EnvironmentObject private var replyWatcher: StreamReplyWatcher
    @EnvironmentObject private var router: DeepLinkRouter
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
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

    /// Which reasoning rows are expanded (collapsed by default; the per-project
    /// "expand by default" comes from Settings). View-side state: the store is
    /// protocol-stable this phase, and expansion is a display concern.
    @State private var expandedReasoningIDs: Set<String> = []

    /// Stick-to-bottom state: how many rows arrived while the operator was
    /// scrolled away from the tail (spec §4 "N new ↓").
    @State private var rowsWhileScrolled: Int = 0
    @State private var nearBottom = true
    /// The row count at the last check — the delta is what counts as "new".
    @State private var lastRowCount = 0
    /// Whether the live-status sheet is presented (nav-bar dot tap).
    @State private var showStatusSheet = false

    init(project: String) {
        self.project = project
        // `project` is captured; the @StateObject is created here so the store
        // (and its persisted cursor/draft) is stable across body recomputes.
        _store = StateObject(wrappedValue: StreamingChatStore(project: project))
    }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(spacing: 0) {
                    if store.rows.isEmpty {
                        if case .failed = store.phase {
                            failedState
                                .padding(.top, Theme.Spacing.page.rawValue)
                        } else {
                            emptyState
                                .padding(.top, Theme.Spacing.page.rawValue)
                        }
                    } else {
                        ForEach(store.rows) { row in
                            rowView(row)
                                .id(row.id)
                        }
                    }
                }
                // 16 gutters (spec §4), no boxed padding per row.
                .padding(.horizontal, Theme.Spacing.lg.rawValue)
                .padding(.top, Theme.Spacing.md.rawValue)
                .padding(.bottom, Theme.Spacing.md.rawValue)
            }
            // Stick-to-bottom gate (replaces the per-token animated scrollTo):
            // follow the tail only while the operator is near the bottom; when
            // they have scrolled up to read, new rows count up into the pill
            // instead of yanking VoiceOver rotor focus (spec §4 + §9).
            .onScrollGeometryChange(for: CGFloat.self) { geo in
                geo.contentSize.height - geo.contentOffset.y - geo.containerSize.height
            } action: { _, distanceFromBottom in
                let wasNear = nearBottom
                nearBottom = distanceFromBottom <= 80
                if !wasNear && nearBottom { rowsWhileScrolled = 0 }
            }
            .onChange(of: liveTick) { _, _ in
                followTail(proxy: proxy)
            }
            .overlay(alignment: .bottomTrailing) {
                newRowsPill(proxy: proxy)
            }
        }
        // The whole live-status story (was the stacked banner) now lives in a
        // 9 pt nav-bar dot; tap opens the one-line status sheet.
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button { showStatusSheet = true } label: {
                    Image(systemName: "circle.fill")
                        .font(.system(size: 9))
                        .foregroundColor(dotColor)
                }
                .accessibilityLabel(dotLabel)
            }
            ToolbarItem(placement: .topBarTrailing) {
                // Read-back pager (t_2776ea3c) — the nav bar's one contextual
                // action (spec §4: ≤1 action), the same history the project
                // Overview offers.
                NavigationLink {
                    SessionHistoryView(client: makeClient(), project: project)
                } label: {
                    Label("Session History", systemImage: "clock.arrow.circlepath")
                }
            }
        }
        .navigationTitle(project)
        .navigationBarTitleDisplayMode(.inline)
        .sheet(isPresented: $showStatusSheet) {
            statusSheet
                .presentationDetents([.height(140)])
        }
        // Turn pill + composer ride the bottom safe-area inset, so the
        // transcript reflows ABOVE the keyboard instead of competing with it.
        .safeAreaInset(edge: .bottom, spacing: 0) {
            VStack(spacing: Theme.Spacing.xs.rawValue) {
                if store.turnState != .idle {
                    turnPill
                }
                if let err = store.sendError {
                    Text(err)
                        .font(.footnote)
                        .foregroundColor(Theme.Semantic.bad)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                composer
            }
            .padding(.horizontal, Theme.Spacing.md.rawValue)   // composer inset 12 (spec §2.3)
            .padding(.top, Theme.Spacing.xs.rawValue)
            .padding(.bottom, Theme.Spacing.sm.rawValue)
            .background(Theme.Semantic.surface)
        }
        .onAppear {
            // Opening this chat declares it the reading project (suppresses
            // badging a reply that lands while it is on screen) and clears any
            // badge that was waiting. Wire the live-stream watermark to the
            // shared watcher so replies seen here are never re-badged later.
            unread.setReading(project)
            unread.markRead(project: project)
            lastRowCount = store.rows.count
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
        // Reflect stream changes onto the session Live Activity: whenever
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

    private func makeClient() -> HSCCClient? {
        guard settings.isConfigured,
              let token = settings.token,
              let port = Int(settings.port) else { return nil }
        return HSCCClient(host: settings.host, port: port, token: token)
    }

    // MARK: - Live dot (nav bar) + status sheet

    private var dotColor: Color {
        switch store.phase {
        case .connected: return Theme.Semantic.ok
        case .connecting, .reconnecting, .idle, .loadingHistory: return Theme.Semantic.warn
        case .failed: return Theme.Semantic.bad
        }
    }

    /// Connection state announced, never colour-only (spec §9): the exact
    /// §4 label while live, honest one-liners otherwise.
    private var dotLabel: String {
        switch store.phase {
        case .connected: return "Live — connected, receiving"
        case .connecting, .idle: return "Connecting to the live stream"
        case .loadingHistory: return "Loading session history"
        case .reconnecting: return "Reconnecting — resuming from latest"
        case .failed(let reason): return "Stream failed: \(reason). Tap for details."
        }
    }

    private var statusSheet: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.md.rawValue) {
            HStack(spacing: Theme.Spacing.sm.rawValue) {
                Circle().fill(dotColor).frame(width: 9, height: 9)
                Text(dotLabel)
                    .font(.subheadline.weight(.semibold))
                    .foregroundColor(Theme.Semantic.onSurface)
            }
            Text("This is a live window onto \(project)'s session — messages, tool calls and card moves stream in as the orchestrator works.")
                .font(.footnote)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
            Spacer()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(Theme.Spacing.lg.rawValue)
    }

    // MARK: - Stick-to-bottom gate

    /// A cheap Equatable signature of the live tail — changes when a new row
    /// lands OR when the last row streams more text. Drives tail-following.
    private var liveTick: String {
        guard let last = store.rows.last else { return "empty" }
        if case .message(_, let text, _) = last.item { return "\(last.id)|\(text.count)" }
        return last.id
    }

    /// Follow the tail only while the operator is near the bottom (within the
    /// ~80 pt gate). Scrolled-up readers get a count instead of a scroll yank.
    /// Reduce Motion: jump, never animate (spec §9).
    private func followTail(proxy: ScrollViewProxy) {
        // Count rows that arrived while scrolled away, for the "N new" pill.
        let count = store.rows.count
        if count > lastRowCount && !nearBottom {
            rowsWhileScrolled += count - lastRowCount
        }
        lastRowCount = count
        guard nearBottom, let last = store.rows.last else { return }
        if reduceMotion {
            proxy.scrollTo(last.id, anchor: .bottom)
        } else {
            withAnimation(.easeOut(duration: 0.15)) {
                proxy.scrollTo(last.id, anchor: .bottom)
            }
        }
    }

    @ViewBuilder
    private func newRowsPill(proxy: ScrollViewProxy) -> some View {
        if rowsWhileScrolled > 0, let last = store.rows.last {
            Button {
                rowsWhileScrolled = 0
                nearBottom = true
                if reduceMotion {
                    proxy.scrollTo(last.id, anchor: .bottom)
                } else {
                    withAnimation(.easeOut(duration: 0.15)) {
                        proxy.scrollTo(last.id, anchor: .bottom)
                    }
                }
            } label: {
                // The arrow is decoration; the label carries the meaning —
                // "Chevron down" must never be what VoiceOver reads (§9).
                Text("\(rowsWhileScrolled) new ↓")
                    .font(.caption.weight(.semibold))
                    .foregroundColor(Theme.Semantic.onAccent)
                    .padding(.horizontal, Theme.Spacing.md.rawValue)
                    .padding(.vertical, Theme.Spacing.xs.rawValue)
                    .background(Capsule().fill(Theme.Semantic.accent))
                    .accessibilityLabel("\(rowsWhileScrolled) new messages. Scroll to the latest.")
            }
            .padding(.trailing, Theme.Spacing.lg.rawValue)
            .padding(.bottom, Theme.Spacing.md.rawValue)
        }
    }

    // MARK: - Turn pill (spec §4: one 24 pt pill, only while a turn runs)

    private var turnPill: some View {
        HStack(spacing: Theme.Spacing.sm.rawValue) {
            if store.turnState == .working {
                ProgressView().controlSize(.small)
            } else {
                Image(systemName: "hand.raised")
                    .font(.caption)
            }
            Text(turnText)
                .font(.caption.weight(.semibold))
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .foregroundColor(Theme.Semantic.warn)
        .padding(.horizontal, Theme.Spacing.md.rawValue)
        .frame(height: 24)
        .frame(maxWidth: .infinity)
        .background(Capsule().fill(Theme.Semantic.tintOverlay(Theme.Semantic.warn)))
        .accessibilityLabel(turnText)
    }

    private var turnText: String {
        switch store.turnState {
        case .idle: return ""
        case .working: return "working…"
        case .awaitingApproval:
            return "waiting for approval on your computer — approve the handoff there to continue"
        }
    }

    // MARK: - Rows (the hairline transcript)

    @ViewBuilder
    private func rowView(_ row: ChatRow) -> some View {
        switch row.item {
        case .message(let role, let text, let streaming):
            if role == "user" {
                UserBubble(text: text, streaming: streaming)
            } else {
                ReplyRow(text: text, streaming: streaming)
            }
        case .tool(let t):
            ToolRow(render: t, rowID: row.id, store: store)
        case .card(let c):
            CardRow(row: c)
        case .agent(let a):
            AgentRow(row: a)
        case .system(let s):
            systemRow(row: row, system: s)
        case .error(let e):
            ErrorRow(error: e)
        case .unknown(let type, let raw):
            UnknownRow(type: type, raw: raw)
        case .notice(let text):
            NoticeRow(text: text)
        }
    }

    /// A `system` frame splits three ways (spec §4): the reasoning text gets
    /// its own collapsible row; the `via=` routing fact gets the centred
    /// notice row; everything else (cron fired, crash, escalation, compaction)
    /// stays a quiet hairline system row.
    @ViewBuilder
    private func systemRow(row: ChatRow, system: SystemPayload) -> some View {
        if system.kind == "reasoning",
           let text = system.details?["text"]?.string, !text.isEmpty {
            ReasoningRow(rowID: row.id, text: text,
                         isExpanded: expandedReasoningIDs.contains(row.id)) {
                toggleReasoning(row.id)
            }
        } else if system.kind == "working",
                  let via = system.details?["via"]?.string, !via.isEmpty {
            ViaNoticeRow(via: via, pane: system.details?["pane"]?.string)
        } else {
            SystemRow(system: system)
        }
    }

    private func toggleReasoning(_ id: String) {
        if expandedReasoningIDs.contains(id) {
            expandedReasoningIDs.remove(id)
        } else {
            expandedReasoningIDs.insert(id)
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
                .padding(.horizontal, Theme.Spacing.xl.rawValue)
        }
    }

    /// Distinct state for a stream that COULD NOT load — shown instead of the
    /// live-chat invite so a failure never masquerades as an idle, working
    /// chat. A real Retry (f5b6ea2c) — the old text promised a pull that
    /// existed nowhere.
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
                    .padding(.horizontal, Theme.Spacing.xl.rawValue)
            }
            Text("Check Settings → Host, Port, and Token, then retry.")
                .font(.footnote)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                .multilineTextAlignment(.center)
                .padding(.horizontal, Theme.Spacing.xl.rawValue)
            Button(action: retryStream) {
                Label("Retry", systemImage: "arrow.clockwise")
                    .font(.subheadline.weight(.semibold))
            }
            .buttonStyle(.borderedProminent)
            .tint(Theme.Semantic.accent)
            .foregroundColor(Theme.Semantic.onAccent)
            .accessibilityHint("Reopens the session stream for \(project).")
        }
    }

    /// Reconnect from the failed state using only the store's public
    /// lifecycle: `stop()` tears the socket down and clears `isActive`, so
    /// `start()` rebuilds a fresh window (re-seed from history, reopen the
    /// socket) exactly as a fresh appearance would. The stream files stay
    /// untouched.
    private func retryStream() {
        store.stop()
        startStream()
    }

    // MARK: - Composer

    private var composer: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs.rawValue) {
            // Slash-command palette (server-sourced command catalog) at a "/"
            // command position; selecting a command inserts it into the draft.
            SlashCommandPalette(draft: $store.draft, client: computedClient)

            // Slash-command PREVIEW: once a command name is committed in the
            // draft ("/name " or "/name args…"), show what it WILL do — what it
            // targets, what it changes, and whether it is destructive — with an
            // explicit Run. Destructive commands get the confirm word appended
            // behind the scenes; read-only / non-destructive stay one-tap.
            SlashPreviewCard(draft: store.draft, client: computedClient) { text in
                store.send(text)
            }

            HStack(alignment: .bottom, spacing: Theme.Spacing.sm.rawValue) {
                TextField("Ask \(project)…", text: $store.draft, axis: .vertical)
                    .lineLimit(1...4)
                    .textFieldStyle(.plain)
                    .padding(.horizontal, Theme.Spacing.md.rawValue)
                    .padding(.vertical, Theme.Spacing.sm.rawValue)
                    .background(
                        RoundedRectangle(cornerRadius: 20, style: .continuous)
                            .fill(Theme.Semantic.surfaceRaised)
                    )
                    .focused($composerFocused)
                    .onChange(of: store.draft) { _, _ in
                        store.persistDraft()
                    }

                // Voice / Dictate — focuses the composer field so the system
                // keyboard (with its built-in dictation key) comes up. Tapping
                // the system dictation key captures the operator's speech into
                // this draft. This is the "system dictation affordance": no
                // custom speech pipeline, no new app-level permission.
                Button {
                    startDictation()
                } label: {
                    Image(systemName: "mic.fill")
                        .font(.footnote)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                        .frame(width: 36, height: 36)
                        .contentShape(Circle())
                        .frame(width: 44, height: 44)   // 44 pt target (§9)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Start dictation")
                .accessibilityHint("Focuses the message field and opens the keyboard's dictation key.")

                // The circular 36 pt accent send (spec §4), padded to 44 pt.
                Button {
                    // Pressing send sends — chat messages are neither destructive
                    // nor expensive, so there is no confirm gate. `send` adds the
                    // optimistic user row immediately and clears the composer.
                    store.send(store.draft)
                } label: {
                    Image(systemName: "paperplane.fill")
                        .font(.footnote.weight(.semibold))
                        .frame(width: 36, height: 36)
                        .foregroundColor(Theme.Semantic.onAccent)
                        .background(Circle().fill(canSend ? Theme.Semantic.accent
                                                       : Theme.Semantic.neutral.opacity(0.4)))
                        .contentShape(Circle())
                        .frame(width: 44, height: 44)   // 44 pt target (§9)
                }
                .buttonStyle(.plain)
                .disabled(!canSend)
                .accessibilityLabel("Send")
            }
        }
    }

    /// The client used by the slash palette and the history link, from current
    /// settings (nil when unconfigured — the palette then stays hidden).
    private var computedClient: HSCCClient? {
        makeClient()
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
}

// MARK: - Row chrome (the shared hairline recipe, spec §4)

/// One transcript row: full-bleed content with a hairline top rule and the
/// 16 pt gutter. The chat's rows are lines in a transcript, not cards —
/// only the operator's own bubble is filled anywhere in the app.
private struct HairlineRow<Content: View>: View {
    /// Extra top breathing room (turns get more than machine rows). Declared
    /// FIRST so `content` is the last parameter — a trailing closure then
    /// matches it forward (Swift 6 deprecates backward matching).
    var turnGap: Bool = false
    @ViewBuilder let content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs.rawValue) {
            content
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.top, turnGap ? Theme.Spacing.lg.rawValue : Theme.Spacing.md.rawValue)
        .padding(.bottom, Theme.Spacing.sm.rawValue)
        .overlay(alignment: .top) {
            // Hairline inset to the leading gutter (§2.3), not full width.
            Rectangle()
                .fill(Theme.Elevation.hairline)
                .frame(height: 1)
                .padding(.leading, Theme.Spacing.lg.rawValue)
        }
    }
}

// MARK: - Operator bubble (the only filled bubble in the app)

/// The operator's own message: trailing accent bubble, onAccent text, radius
/// 18 with a squared bottom-trailing corner, capped measure (spec §4). It is
/// the ONLY filled bubble in the app — everything else is hairlines + text.
private struct UserBubble: View {
    let text: String
    let streaming: Bool

    var body: some View {
        HStack {
            Spacer(minLength: Theme.Spacing.xl.rawValue)
            Text(text + (streaming ? " ▍" : ""))
                .font(.body)
                .foregroundColor(Theme.Semantic.onAccent)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, Theme.Spacing.md.rawValue)
                .padding(.vertical, Theme.Spacing.sm.rawValue)
                .frame(maxWidth: 560, alignment: .trailing)
                .background(
                    UnevenRoundedRectangle(topLeadingRadius: 18, bottomLeadingRadius: 18,
                                           bottomTrailingRadius: 4, topTrailingRadius: 18,
                                           style: .continuous)
                        .fill(Theme.Semantic.accent)
                )
        }
        .padding(.top, Theme.Spacing.lg.rawValue)
        .padding(.bottom, Theme.Spacing.sm.rawValue)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("You: \(text)")
    }
}

// MARK: - REPL reply (plain text, full width)

/// The orchestrator's reply: no bubble, full width — visually attached to the
/// rail, "the terminal talking" (spec §4).
private struct ReplyRow: View {
    let text: String
    let streaming: Bool

    var body: some View {
        HairlineRow(turnGap: true) {
            Text(text + (streaming ? " ▍" : ""))
                .font(.body)
                .foregroundColor(Theme.Semantic.onSurface)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Orchestrator: \(text)")
    }
}

// MARK: - Reasoning row (new, spec §4)

/// The model's reasoning — server `system` kind=reasoning with details.text.
/// Before this it drowned in SystemRow's caption2 lineLimit(2); it is the
/// thing the operator wants while debugging, so it gets its own collapsible
/// row: hairline-topped, brain glyph + "Reasoning" + char count + chevron,
/// body expands IN PLACE in mono footnote muted. Collapsed by default.
private struct ReasoningRow: View {
    let rowID: String
    let text: String
    let isExpanded: Bool
    let toggle: () -> Void

    var body: some View {
        HairlineRow {
            Button(action: toggle) {
                HStack(spacing: Theme.Spacing.sm.rawValue) {
                    Image(systemName: "brain")
                        .font(.caption)
                        .foregroundColor(Theme.Semantic.neutral)
                    Text("Reasoning")
                        .font(.subheadline)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    Text("\(text.count) chars")
                        .font(.hsccMono(.caption))
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    Spacer()
                    Image(systemName: isExpanded ? "chevron.up" : "chevron.down")
                        .font(.caption2)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)

            if isExpanded {
                Text(text)
                    .font(.hsccMono(.footnote))
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.top, Theme.Spacing.xs.rawValue)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Reasoning, \(text.count) characters")
        .accessibilityValue(isExpanded ? "expanded" : "collapsed")
        .accessibilityAddTraits(.isButton)
        .accessibilityHint(isExpanded ? "Collapse the reasoning text" : "Expand the reasoning text")
    }
}

// MARK: - Tool row (same recipe, flattened card)

/// A tool call: the SAME row recipe and the SAME store-side `expandedToolIDs`
/// as the old ToolChip — glyph + name + duration + chevron, mono Arguments/
/// Result panels on expand. Only the card background and stroke are removed;
/// `running` keeps the spinner and amber chip; a failed result turns the chip
/// danger. (Failure read is best-effort — the wire result is one JSON blob:
/// `"error": null` reads as success, a non-null `"error":` as failure.)
private struct ToolRow: View {
    let render: ToolRender
    let rowID: String
    @ObservedObject var store: StreamingChatStore

    private var isExpanded: Bool { store.expandedToolIDs.contains(rowID) }

    var body: some View {
        HairlineRow {
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
                            if !render.finished {
                                HSStatusChip("running", color: Theme.Semantic.warn)
                            } else if isFailure {
                                HSStatusChip("failed", color: Theme.Semantic.bad)
                            }
                            if let dur = render.duration {
                                Text(String(format: "%.1fs", dur))
                                    .font(.hsccMono(.caption))
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
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)

            if isExpanded {
                expandPanel
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Tool \(render.name), \(render.finished ? (isFailure ? "failed" : "finished") : "running")")
        .accessibilityValue(isExpanded ? "expanded" : "collapsed")
        .accessibilityHint(isExpanded ? "Collapse the tool result" : "Expand the tool result")
    }

    private var icon: String {
        if render.finished {
            if isFailure { return "xmark.circle" }
            return render.duration != nil ? "checkmark.circle" : "hammer"
        }
        return "circle.dotted"
    }

    private var color: Color {
        if !render.finished { return Theme.Semantic.warn }
        return isFailure ? Theme.Semantic.bad : Theme.Semantic.ok
    }

    /// Best-effort failure read on the finish blob (see header comment).
    private var isFailure: Bool {
        guard render.finished, let result = render.result else { return false }
        let text = result.renderInline()
        guard text.contains("\"error\":") else { return false }
        return !text.contains("\"error\": null") && !text.contains("\"error\":null")
    }

    /// A tight one-line summary of what the tool did for the collapsed row.
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
                        .font(.hsccMono(.footnote))
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            if let result = render.result {
                panelSection(render.finished ? "Result" : "…") {
                    Text(result.renderInline())
                        .font(.hsccMono(.footnote))
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
        .padding(.top, Theme.Spacing.xs.rawValue)
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

// MARK: - Card row

/// A kanban change: a tappable hairline row (not prose) — jumps to the card's
/// detail through the app-wide ProjectRoute registration.
private struct CardRow: View {
    let row: CardPayload

    var body: some View {
        NavigationLink(value: ProjectRoute.card(id: row.id)) {
            HairlineRow {
                HStack(spacing: Theme.Spacing.sm.rawValue) {
                    Image(systemName: "square.grid.2x2")
                        .font(.caption)
                        .foregroundColor(Theme.Semantic.neutral)
                    VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
                        Text(row.title)
                            // Card moves are metadata under the conversation —
                            // quieter than prose so they don't compete for the eye.
                            .font(.subheadline)
                            .foregroundColor(Theme.Semantic.onSurface)
                            .lineLimit(2)
                        HStack(spacing: Theme.Spacing.xs.rawValue) {
                            Text(row.board)
                                .font(.hsccMono(.caption2))
                                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                            Text("·")
                                .font(.caption2)
                                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                            Text(row.status)
                                .font(.caption.weight(.semibold))
                                .foregroundColor(statusColor(row.status))
                        }
                    }
                    Spacer()
                    Image(systemName: "chevron.right")
                        .font(.caption2)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
            }
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Card \(row.title), board \(row.board), \(row.status)")
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
    let row: AgentPayload

    var body: some View {
        HairlineRow {
            HStack(spacing: Theme.Spacing.sm.rawValue) {
                Image(systemName: row.action == "finished" ? "person.fill.checkmark" : "person.badge.plus")
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.ok)
                VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
                    HStack(spacing: Theme.Spacing.xs.rawValue) {
                        Text(row.role)
                            .font(.subheadline.weight(.semibold))
                            .foregroundColor(Theme.Semantic.onSurface)
                        Text(row.action)
                            .font(.caption)
                            .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    }
                    if let task = row.task, !task.isEmpty {
                        Text(task)
                            .font(.caption)
                            .foregroundColor(Theme.Semantic.onSurfaceMuted)
                            .lineLimit(2)
                    }
                }
                Spacer()
            }
        }
        .accessibilityElement(children: .combine)
    }
}

// MARK: - System row

/// An ambient session fact: cron fired, worker crash, escalation, compaction.
/// Reads quietly (muted) so it does not shout over the actual conversation.
/// (Reasoning and `via=` frames split out earlier — see systemRow.)
private struct SystemRow: View {
    let system: SystemPayload

    var body: some View {
        HairlineRow {
            HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.sm.rawValue) {
                Image(systemName: systemIcon)
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.neutral)
                VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
                    Text(system.kind)
                        .font(.caption.weight(.semibold))
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                        .textCase(.uppercase)
                    // A system frame may carry a human `notice` (e.g. the serve
                    // fallback explaining the terminal REPL wasn't in tmux). It
                    // is the one line the operator must actually read, so it
                    // gets its own full-width line, never a truncating dump.
                    if let notice = system.details?["notice"]?.string, !notice.isEmpty {
                        Text(notice)
                            .font(.footnote)
                            .foregroundColor(Theme.Semantic.onSurface)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let d = system.details, !d.isEmpty {
                        Text(d.renderArgs(pretty: false))
                            .font(.hsccMono(.caption))
                            .foregroundColor(Theme.Semantic.onSurfaceMuted)
                            .lineLimit(3)
                    }
                }
                Spacer()
            }
        }
        .accessibilityElement(children: .combine)
    }

    private var systemIcon: String {
        switch system.kind {
        case "cron_fired": return "clock"
        case "worker_crash": return "exclamationmark.octagon"
        case "escalation": return "arrow.up.forward.square"
        case "compaction": return "arrow.down.left.and.arrow.up.right"
        case "session_rotated": return "arrow.triangle.2.circlepath"
        case "working": return "ellipsis"                    // turn in progress
        case "handoff_pending": return "hand.raised"          // awaiting approval (t_7cc2e7d5)
        case "session_busy": return "lock.fill"               // session busy (fallback)
        default: return "gearshape"
        }
    }
}

// MARK: - Via notice row (spec §4: promoted from the caption2 dump)

/// "via tmux · smoke-pom:0.0" — the routing fact the operator reads when the
/// turn didn't go through the expected path (2.7.0's notice contract). Centred
/// footnote, hairline rules left and right.
private struct ViaNoticeRow: View {
    let via: String
    let pane: String?

    var body: some View {
        HStack(spacing: Theme.Spacing.md.rawValue) {
            Rectangle().fill(Theme.Elevation.hairline).frame(height: 1)
            Text(line)
                .font(.footnote)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                .multilineTextAlignment(.center)
            Rectangle().fill(Theme.Elevation.hairline).frame(height: 1)
        }
        .padding(.vertical, Theme.Spacing.sm.rawValue)
        .accessibilityElement(children: .combine)
    }

    private var line: String {
        var parts = ["via \(via)"]
        if let pane, !pane.isEmpty { parts.append(pane) }
        return parts.joined(separator: " · ")
    }
}

// MARK: - Error row

/// A named, actionable failure. The name (`code`) is shown so the operator
/// can recognize and fix it; the message says what to do next. Hairline row,
/// danger-coloured text — no box, no stroke (the colour + the word carry it).
private struct ErrorRow: View {
    let error: ErrorPayload

    var body: some View {
        HairlineRow {
            HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.sm.rawValue) {
                Image(systemName: "exclamationmark.triangle.fill")
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.bad)
                VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
                    Text(error.code)
                        .font(.subheadline.weight(.semibold))
                        .foregroundColor(Theme.Semantic.bad)
                    Text(error.message)
                        .font(.footnote)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer()
            }
        }
        .accessibilityElement(children: .combine)
    }
}

// MARK: - Unknown row

/// An event type this build does not know (newer backend). Surfaces the raw
/// text so nothing is dropped silently.
private struct UnknownRow: View {
    let type: String
    let raw: String

    var body: some View {
        HairlineRow {
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
                    .font(.hsccMono(.caption))
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    .textSelection(.enabled)
            }
        }
        .accessibilityElement(children: .combine)
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
