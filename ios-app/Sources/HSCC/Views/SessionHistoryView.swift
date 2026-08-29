import SwiftUI

/// Session history — the project's chat log as a WINDOW onto the session.
///
/// This is the history pager (t_2776ea3c). On open it fetches the NEWEST page
/// of the project's session events (`GET /v1/projects/{name}/session/events`
/// with no `before`) so the operator sees context that predates this install —
/// the thing that makes it a SESSION rather than a log the app happens to own.
/// Scrolling UP pages further BACK via the returned `next_before` cursor
/// (strictly older seq), prepending older frames as they arrive.
///
/// Layout: newest event at the BOTTOM, scroll anchored there on open; older
/// frames appear above as the operator scrolls up (mirroring a chat window,
/// where the present is at the bottom and the past recedes upward). Event
/// `seq` runs ascending oldest→newest in the model and is rendered in reverse.
struct SessionHistoryView: View {
    let client: HSCCClient?
    let project: String

    /// Accumulated events, seq-ASCENDING (index 0 = oldest loaded so far).
    @State private var events: [SessionEvent] = []
    /// Cursor for the next OLDER page; nil when we've reached the oldest frame.
    @State private var nextBefore: Int?
    /// The session high-water mark (seq the NEXT event will get).
    @State private var highWaterSeq: Int?
    /// The oldest retained seq on the server (for the "whole history" footer).
    @State private var oldestSeq: Int?

    private enum Phase {
        case idle, loadingTail, ready, loadingOlder, failed(String)
    }
    @State private var phase: Phase = .idle
    /// Prevent concurrent paging requests (scroll triggers can fire back to back).
    @State private var pagingLock = false

    private static let pageLimit = 100

    var body: some View {
        Group {
            switch phase {
            case .idle, .loadingTail:
                HSLoading("Loading session…")
            case .failed(let message):
                HSError("Couldn't load the session", message: message) {
                    phase = .idle
                    events = []
                    Task { await loadTail() }
                }
            case .ready, .loadingOlder:
                if events.isEmpty {
                    emptyState
                } else {
                    history
                }
            }
        }
        .navigationTitle("Session History")
        .navigationBarTitleDisplayMode(.inline)
        .task {
            if events.isEmpty, !isLoading(phase) {
                await loadTail()
            }
        }
    }

    private var history: some View {
        // Reversed: newest (highest seq) at the bottom. Scroll starts there.
        ScrollView {
            LazyVStack(spacing: Theme.Spacing.sm.rawValue) {
                // "Load earlier" — belt and suspenders alongside the auto
                // trigger, so the operator always has an explicit affordance.
                if nextBefore != nil {
                    loadEarlierButton
                }
                ForEach(events.reversed()) { event in
                    EventRow(event: event)
                        .onAppear {
                            // Auto-page: reaching the OLDEST loaded row means
                            // the operator scrolled back to the top of history.
                            if event.seq == events.first?.seq {
                                Task { await loadOlder() }
                            }
                        }
                }
            }
            .padding(.horizontal, Theme.Spacing.md.rawValue)
            .padding(.vertical, Theme.Spacing.lg.rawValue)
        }
        .defaultScrollAnchor(.bottom)
    }

    private var loadEarlierButton: some View {
        Button {
            Task { await loadOlder() }
        } label: {
            HStack(spacing: Theme.Spacing.xs.rawValue) {
                if isLoading(phase) {
                    ProgressView()
                } else {
                    Image(systemName: "arrow.up")
                }
                Text("Older . . .")
            }
            .font(.caption)
            .foregroundColor(Theme.Semantic.onSurfaceMuted)
        }
        .disabled(isLoading(phase))
        .padding(.vertical, Theme.Spacing.xs.rawValue)
    }

    private var emptyState: some View {
        HSEmpty("No session events yet",
                message: "Nothing has been logged for \(project). As the orchestrator runs, its chat activity lands here and you can page back through it.",
                systemImage: "text.bubble")
    }

    // MARK: - Paging

    private func isLoading(_ p: Phase) -> Bool {
        if case .loadingTail = p { return true }
        if case .loadingOlder = p { return true }
        return false
    }

    /// Fetch the newest page (the tail) — called once on open.
    private func loadTail() async {
        guard let client else { return }
        phase = .loadingTail
        do {
            let page = try await client.sessionEvents(project: project, limit: Self.pageLimit)
            events = page.events
            nextBefore = page.next_before
            highWaterSeq = page.next_seq
            oldestSeq = page.oldest_seq
            phase = .ready
        } catch {
            let msg = (error as? HSCCError)?.localizedDescription ?? "Something went wrong."
            phase = .failed(msg)
        }
    }

    /// Fetch an OLDER page (`before` = oldest seq loaded so far) and prepend.
    private func loadOlder() async {
        guard let client, let cursor = nextBefore else { return }
        guard !pagingLock else { return }
        pagingLock = true
        phase = .loadingOlder
        defer { pagingLock = false }
        do {
            let page = try await client.sessionEvents(project: project, before: cursor, limit: Self.pageLimit)
            // Prepend only frames strictly older than what we already hold,
            // so a re-entrant paging response can never duplicate a row.
            let newestHeld = events.first?.seq
            let older = page.events.filter { newestHeld == nil || $0.seq < newestHeld! }
            if !older.isEmpty {
                events.insert(contentsOf: older, at: 0)
            }
            nextBefore = page.next_before
            highWaterSeq = page.next_seq
            oldestSeq = page.oldest_seq
            phase = .ready
        } catch {
            // Keep current history on screen; surface the paging failure inline.
            phase = .ready
            // TODO: inline retry affordance for the failed older-page fetch.
        }
    }
}

/// One session event rendered as a timeline row. Each wire `type` gets a
/// distinct, scannable treatment built from its decoded payload.
private struct EventRow: View {
    let event: SessionEvent

    var body: some View {
        HStack(alignment: .top, spacing: Theme.Spacing.sm.rawValue) {
            gutter
            content
        }
    }

    /// The leading seq + glyph column (monospaced seq, type glyph).
    private var gutter: some View {
        VStack(alignment: .trailing, spacing: Theme.Spacing.xxs.rawValue) {
            Text("#\(event.seq)")
                .font(.hsccMono(11))
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
            Image(systemName: glyph)
                .font(.system(size: 12))
                .foregroundColor(glyphColor)
                .frame(width: 18)
        }
        .frame(width: 46, alignment: .trailing)
    }

    @ViewBuilder
    private var content: some View {
        switch event.payload {
        case .hello(let p):
            HStack(spacing: Theme.Spacing.xs.rawValue) {
                Text("session opened — continue from seq")
                Text("#\(p.next_seq)").font(.hsccMono(13))
            }
            .font(.caption)
            .foregroundColor(Theme.Semantic.onSurfaceMuted)
        case .message(let p):
            messageBubble(p)
        case .toolCall(let p):
            toolRow(p)
        case .card(let p):
            cardRow(p)
        case .agent(let p):
            agentRow(p)
        case .system(let p):
            systemRow(p)
        case .error(let p):
            errorRow(p)
        case .unknown(let type, let raw):
            unknownRow(type: type, raw: raw)
        }
    }

    // MARK: - Rows

    @ViewBuilder
    private func messageBubble(_ p: MessagePayload) -> some View {
        let isUser = p.role.lowercased() == "user"
        HStack {
            if isUser { Spacer(minLength: 48) }
            Text(p.delta)
                .font(.body)
                .foregroundColor(Theme.Semantic.onSurface)
                .padding(.horizontal, Theme.Spacing.md.rawValue)
                .padding(.vertical, Theme.Spacing.sm.rawValue)
                .background(bubbleFill(isUser), in: RoundedRectangle(cornerRadius: Theme.Corner.badge.rawValue, style: .continuous))
            if !isUser { Spacer(minLength: 48) }
        }
    }

    @ViewBuilder
    private func toolRow(_ p: ToolCallPayload) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.xs.rawValue) {
            Image(systemName: p.isStart ? "wrench.and.screwdriver.fill" : "checkmark.seal.fill")
                .font(.system(size: 12))
                .foregroundColor(p.isStart ? Theme.Semantic.neutral : Theme.Semantic.ok)
            Text(p.name)
                .font(.hsccMono(13))
                .foregroundColor(Theme.Semantic.onSurface)
            Text(p.isStart ? "started" : "finished")
                .font(.caption)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
            if let d = p.duration_s {
                Text(String(format: "%.1fs", d))
                    .font(.hsccMono(12))
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
            Spacer(minLength: 0)
        }
    }

    @ViewBuilder
    private func cardRow(_ p: CardPayload) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.xs.rawValue) {
            Image(systemName: "square.grid.2x2.fill")
                .font(.system(size: 12))
                .foregroundColor(cardStatusColor(p.status))
            Text(p.title)
                .font(.body)
                .foregroundColor(Theme.Semantic.onSurface)
                .lineLimit(2)
            Text(p.status)
                .font(.hsccMono(12))
                .foregroundColor(cardStatusColor(p.status))
            Spacer(minLength: 0)
        }
    }

    @ViewBuilder
    private func agentRow(_ p: AgentPayload) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.xs.rawValue) {
            Image(systemName: p.isFinished ? "person.fill.checkmark" : "person.badge.plus")
                .font(.system(size: 12))
                .foregroundColor(p.isFinished ? Theme.Semantic.neutral : Theme.Semantic.ok)
            Text(p.role)
                .font(.hsccMono(13))
                .foregroundColor(Theme.Semantic.onSurface)
            Text(p.isFinished ? "finished" : "spawned")
                .font(.caption)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
            if let task = p.task, !task.isEmpty {
                Text("· \(task)")
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    .lineLimit(1)
            }
            Spacer(minLength: 0)
        }
    }

    @ViewBuilder
    private func systemRow(_ p: SystemPayload) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.xs.rawValue) {
            Image(systemName: "bell.badge.fill")
                .font(.system(size: 12))
                .foregroundColor(Theme.Semantic.warn)
            Text(systemLabel(p.kind))
                .font(.caption)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
            Spacer(minLength: 0)
        }
    }

    @ViewBuilder
    private func errorRow(_ p: ErrorPayload) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.xs.rawValue) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.system(size: 12))
                .foregroundColor(Theme.Semantic.bad)
            Text(p.message)
                .font(.caption)
                .foregroundColor(Theme.Semantic.onSurface)
            Text(p.code)
                .font(.hsccMono(12))
                .foregroundColor(Theme.Semantic.bad)
            Spacer(minLength: 0)
        }
    }

    @ViewBuilder
    private func unknownRow(type: String, raw: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.xs.rawValue) {
            Image(systemName: "questionmark.circle")
                .font(.system(size: 12))
                .foregroundColor(Theme.Semantic.neutral)
            Text("\(type) event")
                .font(.caption)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
            if !raw.isEmpty {
                Text(raw)
                    .font(.hsccMono(11))
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    .lineLimit(2)
            }
            Spacer(minLength: 0)
        }
    }

    // MARK: - Helpers

    private var glyph: String {
        switch event.payload {
        case .hello: return "hand.wave"
        case .message(let p): return p.role.lowercased() == "user" ? "person.fill" : "sparkles"
        case .toolCall: return "wrench.and.screwdriver"
        case .card: return "square.grid.2x2"
        case .agent: return "person.2"
        case .system: return "bell"
        case .error: return "exclamationmark.triangle"
        case .unknown: return "questionmark"
        }
    }

    private var glyphColor: Color {
        switch event.payload {
        case .message(let p): return p.role.lowercased() == "user" ? Theme.Semantic.neutral : Theme.Semantic.ok
        case .toolCall(let p): return p.isStart ? Theme.Semantic.neutral : Theme.Semantic.ok
        case .card(let p): return cardStatusColor(p.status)
        case .agent: return Theme.Semantic.ok
        case .system: return Theme.Semantic.warn
        case .error: return Theme.Semantic.bad
        case .hello, .unknown: return Theme.Semantic.neutral
        }
    }

    private func bubbleFill(_ isUser: Bool) -> Color {
        isUser ? Theme.Semantic.surfaceElevated : Theme.Semantic.surfaceRaised
    }

    private func cardStatusColor(_ status: String) -> Color {
        switch status {
        case "running", "in_progress", "claimed": return Theme.Semantic.ok
        case "blocked", "review", "new": return Theme.Semantic.warn
        case "done", "merged", "closed": return Theme.Semantic.ok
        case "error", "failed", "failing": return Theme.Semantic.bad
        default: return Theme.Semantic.neutral
        }
    }

    private func systemLabel(_ kind: String) -> String {
        switch kind {
        case "cron": return "cron fired"
        case "worker_crash": return "worker crashed"
        case "escalation": return "escalation"
        case "compaction": return "session compacted"
        case "session_rotated": return "session rotated"
        case "gateway": return "gateway"
        default: return "system · \(kind)"
        }
    }
}
