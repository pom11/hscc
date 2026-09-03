import SwiftUI

/// Projects — the PRIMARY tab (new project-centric IA).
///
/// Lists the dozen projects from GET /v1/projects. Tapping a project opens a
/// detail screen (`ProjectDetailView`) with segmented sections:
/// Overview · Chat · Board · Settings. This is the surface the operator
/// actually cares about — everything flows from a project.
///
/// Read-only: navigation + pull-to-refresh only. Follows the LoadState pattern
/// (settings-provided client, honest error handling).
struct ProjectsView: View {
    let client: HSCCClient?

    @EnvironmentObject private var unread: ProjectUnreadCenter

    @State private var projects = LoadState<ProjectsResponse>.idle
    /// Whether the cross-project search sheet is presented.
    @State private var showSearch = false

    var body: some View {
        NavigationStack {
            Group {
                // Connection banner sits BELOW the nav bar, inside the content —
                // never .safeAreaInset(edge: .top) on the stack root, which
                // would draw over the nav bar & toolbar (t_4889e978).
                ConnectionBanner()
                if let client {
                    content(client)
                } else {
                    notConfiguredView
                }
            }
            .navigationTitle("Projects")
            .task {
                // First load: fetch once the view appears (idle → loading).
                if client != nil, projects.value == nil, !projects.isLoading {
                    await load()
                }
            }
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button {
                        showSearch = true
                    } label: {
                        Image(systemName: "magnifyingglass")
                    }
                    .accessibilityLabel("Search projects")
                }
                ToolbarItem(placement: .primaryAction) {
                    Button {
                        Task { await load() }
                    } label: {
                        Image(systemName: "arrow.clockwise")
                    }
                    .disabled(projects.isLoading)
                    .accessibilityLabel("Refresh projects")
                }
            }
            .sheet(isPresented: $showSearch) {
                SearchView(client: client)
            }
        }
    }

    @ViewBuilder
    private func content(_ client: HSCCClient) -> some View {
        switch projects {
        case .loading, .idle:
            HSLoading("Loading…")
        case .failed(let message):
            HSError("Couldn't load projects", message: message) {
                Task { await load() }
            }
        case .stale(let response, _):
            staleContent(response, client: client)
        case .loaded(let response):
            List {
                Section {
                    Label(response.speak, systemImage: "text.bubble")
                        .font(.hsccMono(15))
                }
                if response.projects.isEmpty {
                    Section {
                        Text("No projects registered.")
                            .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    }
                }
                ForEach(response.projects) { project in
                    NavigationLink {
                        ProjectDetailView(client: client, project: project)
                    } label: {
                        projectRow(project)
                    }
                }
            }
            .refreshable { await load() }
        }
    }

    /// Live-free version of the list, headed by a stale banner instead of the
    /// live `speak` line — same rows, clearly marked as last-known.
    @ViewBuilder
    private func staleContent(_ response: ProjectsResponse, client: HSCCClient) -> some View {
        List {
            Section {
                StaleBanner(age: staleMessage ?? "", reason: "Can't reach the cluster right now.") {
                    Task { await load() }
                }
            }
            if response.projects.isEmpty {
                Section {
                    Text("No projects registered.")
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
            }
            ForEach(response.projects) { project in
                NavigationLink {
                    ProjectDetailView(client: client, project: project)
                } label: {
                    projectRow(project)
                }
            }
        }
        .refreshable { await load() }
    }

    private var staleMessage: String? { projects.staleMessage }

    @ViewBuilder
    private func projectRow(_ project: Project) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 6) {
                Text(project.name)
                    .font(.body.weight(.medium))
                    .foregroundColor(Theme.Semantic.onSurface)
                unreadBadge(project.name)
            }
            HSMetaLine([project.board,
                        project.displayTopic.isEmpty ? nil : "topic \(project.displayTopic)"])
        }
    }

    /// The unread badge for a project row — the app's notification mechanism
    /// (t_267da363). Shown only when there are unread replies waiting; hidden
    /// (empty view) at zero so the list stays clean.
    @ViewBuilder
    private func unreadBadge(_ project: String) -> some View {
        let count = unread.count(for: project)
        if count > 0 {
            Text("\(count) unread")
                .font(.caption2.weight(.bold))
                .foregroundColor(.white)  // theme-allow: white on the amber unread chip, fixed hue
                .padding(.horizontal, 6)
                .padding(.vertical, 2)
                .background(Theme.Semantic.warn, in: Capsule())
                .accessibilityLabel("\(count) unread")
        }
    }

    private var notConfiguredView: some View {
        HSConnectGate(systemImage: "folder", verb: "to see your projects")
    }

    private func load() async {
        guard let client else { return }
        projects = await Offline.load(projects,
                                      cacheKey: EndpointPath.projects,
                                      client: client) {
            try await client.projects()
        }
    }
}

/// One project's detail — the hub for everything about that project.
///
/// Four segmented sections behind a picker:
///   * **Overview** — board counts + git state (GET /v1/projects/{name}).
///   * **Chat**     — OrchestratorChatView fixed to THIS project's orchestrator.
///   * **Board**    — the project's kanban board (cards filtered to its board).
///   * **Settings** — project-level settings SHELL (a follow-up card fills this in).
///
/// Each section owns its own load state, so switching never cross-contaminates.
/// Follow-up cards build depth on top — the navigation shell is established here.
struct ProjectDetailView: View {
    let client: HSCCClient
    let project: Project

    enum Section: String, CaseIterable, Identifiable {
        case overview, chat, board, settings
        var id: String { rawValue }
        var label: String {
            switch self {
            case .overview: return "Overview"
            case .chat: return "Chat"
            case .board: return "Board"
            case .settings: return "Settings"
            }
        }
    }

    @State private var selected: Section = .overview

    var body: some View {
        VStack(spacing: 0) {
            Picker("Section", selection: $selected) {
                ForEach(Section.allCases) { section in
                    Text(section.label).tag(section)
                }
            }
            .pickerStyle(.segmented)
            .padding(.horizontal)
            .padding(.vertical, 8)

            switch selected {
            case .overview: ProjectOverviewView(client: client, name: project.name, onOpenChat: { selected = .chat })
            case .chat:     StreamingChatView(project: project.name)
            case .board:    ProjectBoardView(client: client, board: project.board)
            case .settings: ProjectSettingsView(client: client, name: project.name)
            }
        }
        .navigationTitle(project.name)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                // Session history — the project's chat log as a window onto
                // the session. Loads context that predates this install and
                // pages further back on scroll-up (t_2776ea3c).
                NavigationLink {
                    SessionHistoryView(client: client, project: project.name)
                } label: {
                    Label("Session History", systemImage: "clock.arrow.circlepath")
                }
            }
        }
    }
}

/// Overview section — answers \"what is the state of this project?\" in ~3s.
///
/// Layered so the operator's eye lands on risk first:
///   1. A BLOCKED hero (big, warm) when anything is blocked — the thing that
///      actually needs eyes. Running/ready counts sit quietly beside it.
///   2. Git state — branch + ahead/behind + dirty (how sync'd local is).
///   3. Session health — the orchestrator's compaction headroom so a bloating
///      context is visible before it wedges.
///   4. A tappable last-reply preview that jumps to the Chat section.
///
/// Data: GET /v1/projects/{name} (+ the app's own persisted ChatStore for the
/// last-reply preview). Unreadable in ~3 seconds on purpose: no dense tables,
/// risk-first ordering.
struct ProjectOverviewView: View {
    let client: HSCCClient
    let name: String
    /// Navigate to the Chat section (wired by ProjectDetailView to flip its
    /// segmented picker). The last-reply preview calls this on tap.
    var onOpenChat: () -> Void = {}

    @State private var detail = LoadState<ProjectDetailResponse>.idle

    var body: some View {
        Group {
            switch detail {
            case .loading, .idle:
                HSLoading("Loading…")
            case .failed(let message):
                HSError("Couldn't load project", message: message) {
                    Task { await load() }
                }
            case .stale(let state, let ageMessage):
                content(state, staleMessage: ageMessage)
            case .loaded(let state):
                content(state, staleMessage: nil)
            }
        }
        .task {
            if detail.value == nil, !detail.isLoading { await load() }
        }
    }

    @ViewBuilder
    private func content(_ state: ProjectDetailResponse, staleMessage: String?) -> some View {
        List {
            if let staleMessage {
                Section {
                    StaleBanner(age: staleMessage, reason: "Can't reach the cluster right now.") {
                        Task { await load() }
                    }
                }
            }
            Section {
                Label(state.speak, systemImage: "text.bubble")
                    .font(.hsccMono(15))
            }

            // Compact ident — board + repo so the operator knows exactly which
            // project/board/checkout this overview is for.
            if state.board != nil || state.repo != nil {
                Section("Project") {
                    if let board = state.board, !board.isEmpty {
                        LabeledContent("Board") { Text(board).font(.hsccMono(15)) }
                    }
                    if let repo = state.repo, !repo.isEmpty {
                        LabeledContent("Repo") { Text(repo).lineLimit(1).truncationMode(.middle).font(.hsccMono(15)) }
                    }
                }
            }

            countsSection(state.board_counts)

            if let git = state.git, git.is_repo == true {
                gitSection(git)
            } else {
                Section("Git") {
                    Text("Not a git repo.")
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
            }

            if let health = state.session_health {
                sessionHealthSection(health)
            }

            if let last = lastReply(state) {
                lastReplySection(last)
            }
        }
        .refreshable { await load() }
    }

    // MARK: - Counts (blocked first & loud, running/ready beside it)

    @ViewBuilder
    private func countsSection(_ counts: [String: Int]?) -> some View {
        let blocked = counts?["blocked"] ?? 0
        let running = counts?["running"] ?? 0
        let ready = counts?["ready"] ?? 0
        let total = counts?["total"] ?? (blocked + running + ready)

        Section("Board") {
            if blocked > 0 {
                VStack(alignment: .leading, spacing: 6) {
                    HStack(spacing: 8) {
                        Image(systemName: "hand.raised.fill")
                            .foregroundColor(Theme.Semantic.warn)
                        Text("\(blocked) blocked")
                            .font(.title2.weight(.bold))
                            .foregroundColor(Theme.Semantic.warn)
                    }
                    Text("Needs eyes — tap the Board section to triage.")
                        .font(.caption)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
                .padding(.vertical, 6)
            } else {
                HStack(spacing: 8) {
                    Image(systemName: "checkmark.circle.fill")
                        .foregroundColor(Theme.Semantic.ok)
                    Text("Nothing blocked")
                        .font(.title3.weight(.semibold))
                        .foregroundColor(Theme.Semantic.ok)
                }
                .padding(.vertical, 6)
            }

            HStack(spacing: 16) {
                statPill("Running", value: running, color: Theme.Semantic.warn)
                statPill("Ready", value: ready, color: Theme.Semantic.ok)
                Spacer()
                Text("of \(total) open")
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
            .padding(.top, 4)
        }
    }

    @ViewBuilder
    private func statPill(_ label: String, value: Int, color: Color) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("\(value)")
                .font(.title2.weight(.bold))
                .foregroundColor(color)
            Text(label)
                .font(.caption)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
        }
    }

    // MARK: - Git state (branch + ahead/behind + dirty)

    @ViewBuilder
    private func gitSection(_ git: ProjectGit) -> some View {
        Section("Git") {
            if let branch = git.branch, !branch.isEmpty {
                LabeledContent("Branch") { Text(branch).font(.hsccMono(15)) }
            }
            // Sync: ahead = to push, behind = to pull. Dirty adds local edits.
            let behind = git.behind ?? 0
            let ahead = git.ahead ?? 0
            if behind > 0 || ahead > 0 {
                LabeledContent("vs upstream") {
                    Text(syncText(ahead: ahead, behind: behind)).font(.hsccMono(15))
                        .foregroundColor(behind > 0 ? Theme.Semantic.warn : Theme.Semantic.onSurface)
                }
            }
            LabeledContent("Dirty") {
                HStack(spacing: 6) {
                    if git.dirty == true {
                        Circle().fill(Theme.Semantic.warn).frame(width: 8, height: 8)
                    }
                    Text(git.dirty == true ? "Yes" : "No")
                }
            }
            if let age = git.last_activity_seconds_ago {
                LabeledContent("Last commit") { Text(timeAgo(age)) }
            }
            if let head = git.head, !head.isEmpty {
                LabeledContent("Head") { Text(head.prefix(8)).font(.hsccMono(15)) }
            }
            if let uncommitted = git.uncommitted, !uncommitted.isEmpty {
                LabeledContent("Uncommitted") {
                    Text("\(uncommitted.count) file\(uncommitted.count == 1 ? "" : "s")").font(.hsccMono(15))
                }
            }
        }
    }

    private func syncText(ahead: Int, behind: Int) -> String {
        var parts: [String] = []
        if ahead > 0 { parts.append("\(ahead) ahead") }
        if behind > 0 { parts.append("\(behind) behind") }
        return parts.isEmpty ? "synced" : parts.joined(separator: " · ")
    }

    // MARK: - Session health + compaction headroom

    @ViewBuilder
    private func sessionHealthSection(_ h: ProjectSessionHealth) -> some View {
        let atRisk = h.compaction_at_risk == true
        Section {
            if atRisk {
                HStack(spacing: 8) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundColor(Theme.Semantic.bad)
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Compaction at risk")
                            .font(.headline)
                            .foregroundColor(Theme.Semantic.bad)
                        if let reason = h.reason, !reason.isEmpty {
                            Text(reason)
                                .font(.caption)
                                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                        }
                    }
                }
                .padding(.vertical, 4)
            } else {
                HStack(spacing: 8) {
                    Image(systemName: "arrow.triangle.2.circlepath")
                        .foregroundColor(Theme.Semantic.ok)
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Compaction healthy")
                            .font(.headline)
                            .foregroundColor(Theme.Semantic.ok)
                        if let reason = h.reason, !reason.isEmpty {
                            Text(reason)
                                .font(.caption)
                                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                        }
                    }
                }
                .padding(.vertical, 4)
            }

            if let headroom = compactionHeadroom(h) {
                headroomBar(h, headroom: headroom)
            }
            if let profile = h.profile, !profile.isEmpty {
                LabeledContent("Session") { Text(profile).font(.hsccMono(15)) }
            }
            if let messages = h.messages {
                LabeledContent("Messages") { Text("\(messages)").font(.hsccMono(15)) }
            }
            if let tokens = h.input_tokens {
                LabeledContent("Input tokens") { Text("\(tokens)").font(.hsccMono(15)) }
            }
        } header: {
            Text("Session health")
        } footer: {
            Text(healthFootnote(h))
        }
    }

    /// Fraction (0...1) of context still free before the compaction threshold,
    /// derived from threshold_tokens vs the cumulative input_tokens gauge.
    /// nil when the numbers aren't present / sane, or when the gauge is
    /// meaningless.
    ///
    /// `input_tokens` is a CUMULATIVE counter that compaction never resets, so
    /// once it has crossed `threshold_tokens` it no longer measures "context
    /// used now" — it is just a lifetime total and can sit far above the cap
    /// (e.g. 11.9M vs a 100K threshold) without the session being at risk. In
    /// that regime (tokens >= threshold) there is no live-free-context number to
    /// show; forcing a 0.0 clamp here painted a permanent red "0% headroom" bar
    /// under the green "Compaction healthy" verdict, a false alarm. So return
    /// nil (hide the bar) once cumulative tokens reach the threshold and let the
    /// server's own real signals (compaction_at_risk / bloated) carry the risk.
    private func compactionHeadroom(_ h: ProjectSessionHealth) -> Double? {
        guard let tokens = h.input_tokens, tokens > 0,
              let threshold = h.threshold_tokens, threshold > 0 else { return nil }
        // Lifetime total already past the cap ⇒ not a live headroom measure.
        guard tokens < threshold else { return nil }
        return min(1.0, 1.0 - Double(tokens) / Double(threshold))
    }

    @ViewBuilder
    private func headroomBar(_ h: ProjectSessionHealth, headroom: Double) -> some View {
        let pct = Int((headroom * 100).rounded())
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text("\(pct)% context headroom")
                    .font(.caption.weight(.semibold))
                    .foregroundColor(headroomColor(headroom))
                Spacer()
                if let threshold = h.threshold_tokens {
                    Text("cap \(threshold)")
                        .font(.caption2)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
            }
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule()
                        .fill(Theme.Semantic.surfaceElevated)
                    Capsule()
                        .fill(headroomColor(headroom))
                        .frame(width: geo.size.width * headroom)
                }
            }
            .frame(height: 6)
        }
        .padding(.vertical, 4)
    }

    private func headroomColor(_ headroom: Double) -> Color {
        // Red at the edge, amber approaching it, green healthy.
        switch headroom {
        case ..<0.15: return Theme.Semantic.bad
        case ..<0.35: return Theme.Semantic.warn
        default: return Theme.Semantic.ok
        }
    }

    private func healthFootnote(_ h: ProjectSessionHealth) -> String {
        if h.compaction_at_risk == true { return "Compaction is not clearing context — watch this session." }
        // When not at risk but there's an explicit stall signal, surface it.
        if let streak = h.compression_fallback_streak, streak > 0 {
            return "\(streak) fallback compaction\(streak == 1 ? "" : "s") in a row."
        }
        if let ineffective = h.compression_ineffective_count, ineffective > 0 {
            return "\(ineffective) compaction\(ineffective == 1 ? "" : "s") did not reduce context."
        }
        if let err = h.compression_failure_error, !err.isEmpty {
            return "Last compaction failed: \(err)"
        }
        return "Session context is healthy."
    }

    // MARK: - Last reply preview (tappable → Chat)

    /// The most recent orchestrator reply from the app's own persisted
    /// transcript, if there is one. Read from ChatStore (the SAME store the
    /// Chat section uses) so the preview always matches what Chat shows without
    /// a second network round-trip.
    private func lastReply(_ state: ProjectDetailResponse) -> String? {
        let store = ChatStore(project: name)
        guard let entry = store.transcript.last(where: { if case .reply = $0 { return true }; return false }) else {
            return nil
        }
        return entry.text
    }

    @ViewBuilder
    private func lastReplySection(_ text: String) -> some View {
        Section {
            Button(action: onOpenChat) {
                HStack(alignment: .top, spacing: 10) {
                    ZStack {
                        Circle()
                            .fill(Color.accentColor.opacity(0.15))
                            .frame(width: 30, height: 30)
                        Image(systemName: "bubble.left.and.bubble.right.fill")
                            .font(.system(size: 14))
                            .foregroundColor(Color.accentColor)
                    }
                    VStack(alignment: .leading, spacing: 3) {
                        HStack {
                            Text("Last reply")
                                .font(.subheadline.weight(.semibold))
                                .foregroundColor(Theme.Semantic.onSurface)
                            Spacer()
                            Image(systemName: "chevron.right")
                                .font(.caption.weight(.semibold))
                                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                        }
                        Text(text)
                            .font(.subheadline)
                            .foregroundColor(Theme.Semantic.onSurfaceMuted)
                            .lineLimit(3)
                            .multilineTextAlignment(.leading)
                    }
                }
                .padding(.vertical, 2)
            }
        } header: {
            Text("Chat")
        } footer: {
            Text("Tap to open the chat for this project.")
        }
    }

    private func timeAgo(_ seconds: Int) -> String {
        let interval = TimeInterval(max(0, seconds))
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .abbreviated
        return formatter.localizedString(for: Date(timeIntervalSinceNow: -interval),
                                        relativeTo: Date())
    }

    private func load() async {
        detail = await Offline.load(detail,
                                    cacheKey: "/v1/projects/\(name)",
                                    client: client) {
            try await client.projectDetail(name)
        }
    }
}

/// Board section — THIS project's kanban board (read-only).
///
/// Fetches GET /v1/cards and filters to the project's board, so the operator
/// sees exactly this project's cards in one place. Also brings in the board's
/// blocked (/v1/kanban/blocked) and stale (/v1/kanban/stale) reads, filtered
/// to this project, under a "Needs attention" heading — the states the operator
/// actively cares about.
struct ProjectBoardView: View {
    let client: HSCCClient
    let board: String?

    @State private var cards = LoadState<CardsResponse>.idle
    @State private var blocked = LoadState<KanbanBlockedResponse>.idle
    @State private var stale = LoadState<KanbanStaleResponse>.idle
    @State private var showCreate = false

    var body: some View {
        Group {
            switch cards {
            case .loading, .idle:
                HSLoading("Loading…")
            case .failed(let message):
                HSError("Couldn't load the board", message: message) {
                    Task { await load() }
                }
            case .stale(let response, let ageMessage):
                content(response, staleMessage: ageMessage)
            case .loaded(let response):
                content(response, staleMessage: nil)
            }
        }
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button {
                    showCreate = true
                } label: {
                    Label("New Card", systemImage: "plus")
                }
                .accessibilityLabel("Create a new card on this board")
            }
        }
        .sheet(isPresented: $showCreate) {
            CreateCardSheet(client: client, board: board) {
                // A card was created — refresh so it shows up in the open list.
                Task { await load() }
            }
        }
        .task {
            if cards.value == nil, !cards.isLoading { await load() }
        }
    }

    @ViewBuilder
    private func content(_ response: CardsResponse, staleMessage: String?) -> some View {
        // Filter to this project's board. Every card carries a `board`; cards
        // with no board match are excluded (the board section is project-scoped).
        let filtered = response.cards.filter { card in
            guard let board else { return card.board == nil || card.board?.isEmpty == true }
            return card.board == board
        }
        // Blocked / stale scoped to THIS project's board.
        let projectBlocked = blocked.value?.tasks?.filter { $0.board == board } ?? []
        let projectStale = stale.value?.tasks?.filter { $0.board == board } ?? []

        List {
            if let staleMessage {
                Section {
                    StaleBanner(age: staleMessage, reason: "Can't reach the cluster right now.") {
                        Task { await load() }
                    }
                }
            }
            if filtered.isEmpty && projectBlocked.isEmpty && projectStale.isEmpty {
                Section {
                    HSEmpty("No cards on \(board ?? "this") board",
                             message: "Nothing is open here yet. This section fills in as the project's board develops.",
                             systemImage: "square.grid.2x2")
                }
            } else {
                // Active cards first — the primary surface.
                Section("Open cards") {
                    if filtered.isEmpty {
                        Text("No open cards on this board.")
                            .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    } else {
                        ForEach(filtered) { card in
                            NavigationLink {
                                CardDetailView(cardID: card.id)
                            } label: {
                                cardRow(card)
                            }
                        }
                    }
                }

                // Blocked — things stalled that the operator cares about.
                if !projectBlocked.isEmpty {
                    Section("Blocked — \(projectBlocked.count)") {
                        ForEach(projectBlocked, id: \.id) { card in
                            buttonRow(icon: "hand.raised.fill",
                                      color: Theme.Semantic.warn,
                                      title: card.displayTitle,
                                      subtitle: subtitle(blockCard: card))
                        }
                    }
                }

                // Stale — non-terminal cards sitting too long.
                if !projectStale.isEmpty {
                    Section("Stale — \(projectStale.count)") {
                        ForEach(projectStale, id: \.id) { card in
                            buttonRow(icon: "clock.fill",
                                      color: Theme.Semantic.warn,
                                      title: card.displayTitle,
                                      subtitle: subtitle(staleCard: card))
                        }
                    }
                }
            }
        }
        .refreshable { await load() }
    }

    private func subtitle(blockCard card: BlockedCard) -> String {
        var parts: [String] = []
        if let why = card.why, !why.isEmpty { parts.append(why) }
        if card.comments?.isEmpty == false { parts.append("\(card.comments?.count ?? 0) comments") }
        parts.append(ageString(card.age_days, noun: "blocked"))
        return parts.joined(separator: " · ")
    }

    private func subtitle(staleCard card: StaleCard) -> String {
        ageString(card.age_days, noun: "stale")
    }

    private func ageString(_ days: Int?, noun: String) -> String {
        guard let days else { return "age unknown" }
        return days == 0 ? "\(noun) today" : "\(days)d \(noun)"
    }

    @ViewBuilder
    private func buttonRow(icon: String, color: Color, title: String, subtitle: String) -> some View {
        HSStatusRow(title, caption: subtitle, icon: icon, iconColor: color)
    }

    @ViewBuilder
    private func cardRow(_ card: Card) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            HSStatusDot(statusColor(card.displayStatus))
            VStack(alignment: .leading, spacing: 3) {
                Text(card.displayTitle)
                    .foregroundColor(Theme.Semantic.onSurface)
                HSMetaLine([card.displayStatus, card.assignee, card.id])
            }
        }
    }

    private func statusColor(_ status: String) -> Color {
        switch status {
        case "running", "claimed", "in_progress": return Theme.Semantic.ok
        case "review", "blocked": return Theme.Semantic.warn
        case "done", "merged", "closed": return Theme.Semantic.ok
        case "failed", "failing": return Theme.Semantic.bad
        default: return Theme.Semantic.neutral
        }
    }

    private func load() async {
        cards = .loading
        // Load the three reads concurrently so the board fills in fast; each
        // writes to its own LoadState so one failing read never blanks the rest.
        await withTaskGroup(of: Void.self) { group in
            group.addTask { await self.loadCards() }
            group.addTask { await self.loadBlockedIfPresent() }
            group.addTask { await self.loadStaleIfPresent() }
        }
    }

    private func loadCards() async {
        cards = await Offline.load(cards,
                                   cacheKey: EndpointPath.cards,
                                   client: client) {
            try await client.cards()
        }
    }

    /// Load blocked/stale best-effort: if their reads fail we still show the
    /// active cards (the board section must not blank because a hygiene read
    /// hiccuped).
    private func loadBlockedIfPresent() async {
        do {
            blocked = .loaded(try await client.kanbanBlocked())
        } catch {
            blocked = .failed(operatorErrorMessage(error))
        }
    }

    private func loadStaleIfPresent() async {
        do {
            stale = .loaded(try await client.kanbanStale(olderThan: 0))
        } catch {
            stale = .failed(operatorErrorMessage(error))
        }
    }
}

/// Settings section — what the operator can actually see/change for a project.
///
/// The project registry is basically read-only for the operator: repo path,
/// board name, session topic, and the orchestrator profile/session live on the
/// server (provisioned / driven by the project hub), not in the app. There is
/// nothing here the operator can edit from the phone, so everything is
/// presented read-only — honest about what's configurable vs. what is not. No
/// fake editable controls.
///
/// Shows repo path, board name, topic, and the orchestrator's project session.
struct ProjectSettingsView: View {
    let client: HSCCClient
    let name: String

    @State private var detail = LoadState<ProjectDetailResponse>.idle

    var body: some View {
        Group {
            switch detail {
            case .loading, .idle:
                HSLoading("Loading…")
            case .failed(let message):
                HSError("Couldn't load project settings", message: message) {
                    Task { await load() }
                }
            case .stale(let state, let ageMessage):
                content(state, staleMessage: ageMessage)
            case .loaded(let state):
                content(state, staleMessage: nil)
            }
        }
        .task {
            if detail.value == nil, !detail.isLoading { await load() }
        }
    }

    @ViewBuilder
    private func content(_ state: ProjectDetailResponse, staleMessage: String?) -> some View {
        List {
            if let staleMessage {
                Section {
                    StaleBanner(age: staleMessage, reason: "Can't reach the cluster right now.") {
                        Task { await load() }
                    }
                }
            }
            Section {
                Text("These project settings live on the cluster, driven by the project hub. They are read-only from this app.")
                    .font(.footnote)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }

            Section("Repository") {
                row("Path", value: state.repo)
                if let git = state.git, git.is_repo == true, let branch = git.branch, !branch.isEmpty {
                    row("Branch", value: branch)
                }
            }

            Section("Board") {
                row("Name", value: state.board)
                if let counts = state.board_counts, counts["total"] != nil {
                    row("Open cards", value: "\(counts["total"] ?? 0)")
                }
            }

            Section("Orchestrator") {
                // The orchestrator's per-project profile + session follow the
                // project naming convention: `<project>-orch` / session `<project>`.
                row("Profile", value: "\(name)-orch")
                row("Session", value: name)
                if let topic = state.displayTopic, !topic.isEmpty {
                    row("Topic", value: topic)
                }
                NavigationLink {
                    ProfileEditorView(client: client, profile: "\(name)-orch")
                } label: {
                    Label("Edit Bot Profile…", systemImage: "slider.horizontal.3")
                }
            }

            Section {
                Text("The orchestrator session persists context across messages — this conversation is continuous, not one-off. To restart it, recreate the session on the cluster.")
                    .font(.footnote)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
        }
    }

    @ViewBuilder
    private func row(_ label: String, value: String?) -> some View {
        if let value, !value.isEmpty {
            LabeledContent(label) {
                Text(value).font(.hsccMono(15)).textSelection(.enabled)
            }
        }
    }

    private func load() async {
        detail = await Offline.load(detail,
                                    cacheKey: "/v1/projects/\(name)",
                                    client: client) {
            try await client.projectDetail(name)
        }
    }
}
